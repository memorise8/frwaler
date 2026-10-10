"""Resume ONERA's linked press catalogue after verifying source and DB prefixes."""
import hashlib
import json
import os
import re
import time
from urllib.parse import urljoin, urlparse

from delivery.health.access_challenge import access_challenge


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), default=str).encode()).hexdigest()


class OneraTraversal:
    DELIVERY_CURSOR_VERSION = 1
    DELIVERY_STOP_ON_KNOWN = False

    def _press_contract(self):
        return digest(['onera-fr-press-v1', self.base_url, self.START_PAGE, 100])

    def _press_row_digest(self, item):
        postgres = os.environ.get('LIBERTREE_DB_BACKEND', 'sqlite').lower() == 'postgres'
        if not postgres and not self._conn_is_libertree():
            raise ValueError('ONERA cursor requires the delivery database')
        fields = ('seq_id', 'post_number', 'meta_url', 'title', 'abstract', 'pdf_url',
                  'published_date', 'published_date_raw', 'published_date_status',
                  'published_date_precision', 'listed_date', 'listed_date_raw',
                  'listed_date_status', 'listed_date_precision')
        mark = '%s' if postgres else '?'
        rows = self._conn.execute(f'SELECT {",".join(fields)} FROM documents '
                                  f'WHERE site_id={mark} AND meta_url={mark}',
                                  (self.site_id, item['url'])).fetchall()
        if len(rows) != 1:
            raise ValueError('ONERA stored prefix document missing or ambiguous')
        values = [rows[0][k] for k in fields] if hasattr(rows[0], 'keys') else list(rows[0])
        return digest([item['url'], values])

    def _press_page(self, page):
        url = self.base_url + '/fr/presse?page=' + str(page)
        raw = self._fetch(url, context=f'backfill list page {page}')
        if not raw:
            raise ConnectionError('ONERA listing unavailable')
        if access_challenge(raw):
            raise PermissionError('ONERA source access challenge')
        soup = self._soup(raw, context=f'backfill list page {page}')
        if soup is None:
            raise ValueError('ONERA listing invalid')
        items = self._parse_list(soup)
        if not items or len({i['url'] for i in items}) != len(items):
            raise ValueError('ONERA empty or repeated source listing')
        links = soup.find_all('a', rel='next')
        next_page = None
        if links:
            targets = {urljoin(url, a.get('href', '')) for a in links}
            expected = self.base_url + '/fr/presse?page=' + str(page + 1)
            if targets != {expected}:
                raise ValueError('ONERA paginator is not the adjacent source page')
            next_page = page + 1
        for item in items:
            parsed = urlparse(item['url'])
            if (parsed.scheme != 'https' or parsed.netloc != 'www.onera.fr'
                    or not parsed.path.startswith('/fr/presse/') or parsed.query or parsed.fragment
                    or not item['title']):
                raise ValueError('ONERA listing identity outside reviewed scope')
        return dict(items=items, next_page=next_page, page_hash=digest([items, next_page]))

    def _crawl_press_backfill(self, limit, *, max_pages, max_seconds):
        self._check_cancelled()
        if limit is not None and limit <= 0:
            return 0
        self._source_failure, self._exhausted, self._pending_cursor = None, False, None
        self._cursor_items_done = 0
        resume = self.delivery_cursor
        if resume is not None:
            valid = (isinstance(resume, dict) and resume.get('version') == 1
                     and resume.get('contract') == self._press_contract()
                     and type(resume.get('page')) is int and resume['page'] >= self.START_PAGE
                     and type(resume.get('offset')) is int and resume['offset'] >= 0
                     and type(resume.get('page_items')) is int and resume['page_items'] > 0
                     and resume['offset'] <= resume['page_items'])
            if not valid or any(not isinstance(resume.get(k), str) or not re.fullmatch('[0-9a-f]{64}', resume[k])
                                for k in ('page_hash', 'prefix_hash', 'rows_hash')):
                self._report_source_failure('configuration')
                return 0
        saved, scanned, page = 0, 0, self.START_PAGE
        prefix, rows_hash = digest(['onera-source-prefix']), digest(['onera-stored-prefix'])
        reached, seen, started = resume is None, set(), time.monotonic()

        def budget():
            if time.monotonic() - started < max_seconds:
                return False
            if not self.delivery_cursor_items_done:
                self._report_source_failure('invalid_response')
            return True

        def checkpoint(data, offset, done=0):
            self._advance_cursor(dict(version=1, contract=self._press_contract(), page=page,
                                      offset=offset, page_items=len(data['items']),
                                      page_hash=data['page_hash'], prefix_hash=prefix,
                                      rows_hash=rows_hash), items_done=done)

        while True:
            self._check_cancelled()
            if limit is not None and saved >= limit or budget():
                return saved
            if scanned >= max_pages:
                self._report_source_failure('invalid_response')
                return saved
            try:
                data = self._press_page(page)
                scanned += 1
                at_cursor = bool(resume and page == resume['page'])
                if at_cursor and (data['page_hash'] != resume['page_hash']
                                  or len(data['items']) != resume['page_items']
                                  or prefix != resume['prefix_hash']):
                    raise ValueError('ONERA source prefix changed')
                offset = resume['offset'] if at_cursor else 0
                if not reached:
                    for item in data['items'][:offset] if at_cursor else data['items']:
                        self._check_cancelled()
                        if budget():
                            return saved
                        if item['url'] in seen:
                            raise ValueError('ONERA source prefix repeated')
                        seen.add(item['url'])
                        rows_hash = digest([rows_hash, self._press_row_digest(item)])
                    if at_cursor:
                        if rows_hash != resume['rows_hash']:
                            raise ValueError('ONERA stored prefix changed')
                        reached = True
                if reached:
                    checkpoint(data, offset)
                    for index, item in enumerate(data['items'][offset:], offset):
                        self._check_cancelled()
                        if limit is not None and saved >= limit or budget():
                            return saved
                        if item['url'] in seen:
                            raise ValueError('ONERA source identity repeated')
                        seen.add(item['url'])
                        self._last_processed_seq_id = None
                        time.sleep(max(0, self._delay))
                        self._check_cancelled()
                        try:
                            created = self._process_item(item)
                            if self._last_processed_seq_id is None:
                                raise ValueError('ONERA item has no accepted stored body')
                            saved += int(bool(created))
                            rows_hash = digest([rows_hash, self._press_row_digest(item)])
                            checkpoint(data, index + 1, done=1)
                            self._check_cancelled()
                        except Exception:
                            self._report_source_failure('item_failed')
                            return saved
            except PermissionError:
                self._report_source_failure('authentication_failed')
                return saved
            except ConnectionError:
                self._report_source_failure('fetch_failed')
                return saved
            except Exception:
                self._report_source_failure('invalid_response')
                return saved
            if data['next_page'] is None:
                if reached and not self.delivery_source_failure:
                    self._mark_exhausted()
                else:
                    self._report_source_failure('invalid_response')
                return saved
            prefix = digest([prefix, page, data['page_hash']])
            page = data['next_page']
