"""Durable CANURB catalogue traversal with source and stored-row anchors."""
import hashlib
import json
import os
import re
import time
from urllib.parse import urljoin

from delivery.health.access_challenge import access_challenge


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), default=str).encode()).hexdigest()


class CanurbTraversal:
    DELIVERY_CURSOR_VERSION = 1
    DELIVERY_STOP_ON_KNOWN = False

    def _catalogue_contract(self):
        return digest(['canurb-publications-v2', self.base_url + '/publications/',
                       'publisher_cards_including_linked_PDF_and_external_publications'])

    def _catalogue_stored(self, item, *, required):
        postgres = os.environ.get('LIBERTREE_DB_BACKEND', 'sqlite').lower() == 'postgres'
        if not postgres and not self._conn_is_libertree():
            raise ValueError('CANURB cursor requires the delivery database')
        fields = ('seq_id', 'post_number', 'meta_url', 'title', 'abstract', 'pdf_url',
                  'published_date', 'published_date_raw', 'published_date_status',
                  'published_date_precision', 'listed_date', 'listed_date_raw',
                  'listed_date_status', 'listed_date_precision')
        key = item['post_id'] or self._slug_from_url(item['url'])
        mark = '%s' if postgres else '?'
        rows = self._conn.execute(f'SELECT {",".join(fields)} FROM documents '
                                  f'WHERE site_id={mark} AND (post_number={mark} OR meta_url={mark})',
                                  (self.site_id, key, item['url'])).fetchall()
        if not rows and not required:
            return None
        if len(rows) != 1:
            raise ValueError('CANURB stored source identity missing or ambiguous')
        values = [rows[0][k] for k in fields] if hasattr(rows[0], 'keys') else list(rows[0])
        if values[1:3] != [key, item['url']]:
            raise ValueError('CANURB stored source identity changed')
        return digest([key, item['url'], values])

    def _catalogue_page(self, page):
        root = self.base_url + '/publications/'
        url = root if page == 1 else root + f'page/{page}/'
        raw = self._curl_get(url, context=f'backfill list page {page}')
        if not raw:
            raise ConnectionError('CANURB listing unavailable')
        if access_challenge(raw):
            raise PermissionError('CANURB source access challenge')
        soup = self._make_soup(raw)
        if soup is None:
            raise ValueError('CANURB invalid listing')
        items = self._parse_list(soup)
        cards = soup.select("article[class*='publications']")
        keys = [i['post_id'] or self._slug_from_url(i['url']) for i in items]
        if (not items or len(cards) != len(items) or len(set(keys)) != len(items)
                or len({i['url'] for i in items}) != len(items)):
            raise ValueError('CANURB empty, skipped or repeated source cards')
        links = soup.select('a.next.page-numbers, .nav-previous a, a[rel="next"]')
        next_page = None
        if links:
            expected = root + f'page/{page + 1}/'
            if {urljoin(url, n.get('href', '')) for n in links} != {expected}:
                raise ValueError('CANURB next page is not the adjacent catalogue page')
            next_page = page + 1
        return dict(items=items, next_page=next_page, page_hash=digest([items, next_page]))

    def _crawl_catalogue_backfill(self, limit, *, max_pages, max_seconds):
        self._check_cancelled()
        if limit is not None and limit <= 0:
            return 0
        self._source_failure, self._exhausted, self._pending_cursor = None, False, None
        self._cursor_items_done = 0
        resume = self.delivery_cursor
        if resume is not None:
            valid = (isinstance(resume, dict) and resume.get('version') == 1
                     and resume.get('contract') == self._catalogue_contract()
                     and type(resume.get('page')) is int and resume['page'] >= 1
                     and type(resume.get('offset')) is int and resume['offset'] >= 0
                     and type(resume.get('page_items')) is int and resume['page_items'] > 0
                     and resume['offset'] <= resume['page_items'])
            if not valid or any(not isinstance(resume.get(k), str) or not re.fullmatch('[0-9a-f]{64}', resume[k])
                                for k in ('page_hash', 'prefix_hash', 'rows_hash')):
                self._report_source_failure('configuration')
                return 0
        saved, page = 0, 1
        prefix, row_hash = digest(['canurb-source-prefix']), digest(['canurb-stored-prefix'])
        reached, seen_keys, seen_urls, started = resume is None, set(), set(), time.monotonic()

        def budget():
            if time.monotonic() - started < max_seconds:
                return False
            if not self.delivery_cursor_items_done:
                self._report_source_failure('invalid_response')
            return True

        def remember(item):
            key = item['post_id'] or self._slug_from_url(item['url'])
            if key in seen_keys or item['url'] in seen_urls:
                raise ValueError('CANURB source identity repeated across pages')
            seen_keys.add(key)
            seen_urls.add(item['url'])

        def checkpoint(data, offset, done=0):
            self._advance_cursor(dict(version=1, contract=self._catalogue_contract(), page=page,
                                      offset=offset, page_items=len(data['items']),
                                      page_hash=data['page_hash'], prefix_hash=prefix,
                                      rows_hash=row_hash), items_done=done)

        while True:
            self._check_cancelled()
            if limit is not None and saved >= limit or budget():
                return saved
            if page > max_pages:
                self._report_source_failure('invalid_response')
                return saved
            try:
                data = self._catalogue_page(page)
                at_cursor = bool(resume and page == resume['page'])
                if at_cursor and (data['page_hash'] != resume['page_hash']
                                  or len(data['items']) != resume['page_items']
                                  or prefix != resume['prefix_hash']):
                    raise ValueError('CANURB source prefix changed')
                offset = resume['offset'] if at_cursor else 0
                if not reached:
                    for item in data['items'][:offset] if at_cursor else data['items']:
                        self._check_cancelled()
                        if budget():
                            return saved
                        remember(item)
                        row_hash = digest([row_hash, self._catalogue_stored(item, required=True)])
                    if at_cursor:
                        if row_hash != resume['rows_hash']:
                            raise ValueError('CANURB stored prefix changed')
                        reached = True
                if reached:
                    checkpoint(data, offset)
                    for index, item in enumerate(data['items'][offset:], offset):
                        self._check_cancelled()
                        if limit is not None and saved >= limit or budget():
                            return saved
                        remember(item)
                        self._last_processed_seq_id = None
                        try:
                            self._catalogue_stored(item, required=False)
                            created = self._process_record(item)
                            if self._last_processed_seq_id is None:
                                raise ValueError('CANURB record has no accepted stored body')
                            saved += int(bool(created))
                            row_hash = digest([row_hash, self._catalogue_stored(item, required=True)])
                            checkpoint(data, index + 1, done=1)
                            self._check_cancelled()
                        except Exception:
                            self._report_source_failure('item_failed')
                            return saved
                        time.sleep(max(0, self._delay))
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
