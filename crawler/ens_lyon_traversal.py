"""Resume the ENS press catalogue with source, row and exclusion anchors."""
import hashlib
import json
import os
import re
import time
from urllib.parse import urljoin, urlparse, urlunparse

from delivery.health.access_challenge import access_challenge


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), default=str).encode()).hexdigest()


class EnsLyonTraversal:
    DELIVERY_CURSOR_VERSION = 1
    DELIVERY_STOP_ON_KNOWN = False

    def _ens_contract(self):
        return digest(['ens-lyon-press-v1', self.LIST_URL, self.MIN_ABSTRACT_CHARS,
                       'press_PDF_priority', 'short_body_exclusion_revalidated'])

    def _ens_row(self, item, *, required):
        postgres = os.environ.get('LIBERTREE_DB_BACKEND', 'sqlite').lower() == 'postgres'
        if not postgres and not self._conn_is_libertree():
            raise ValueError('ENS cursor requires the delivery database')
        fields = ('seq_id', 'post_number', 'meta_url', 'title', 'abstract', 'pdf_url',
                  'original_filename', 'authors', 'published_date', 'published_date_raw',
                  'published_date_status', 'published_date_precision', 'listed_date',
                  'listed_date_raw', 'listed_date_status', 'listed_date_precision')
        mark = '%s' if postgres else '?'
        rows = self._conn.execute(f'SELECT {",".join(fields)} FROM documents '
                                  f'WHERE site_id={mark} AND (post_number={mark} OR meta_url={mark})',
                                  (self.site_id, item['key'], item['url'])).fetchall()
        if not rows and not required:
            return None
        if len(rows) != 1:
            raise ValueError('ENS stored source identity missing or ambiguous')
        values = [rows[0][k] for k in fields] if hasattr(rows[0], 'keys') else list(rows[0])
        if values[1:3] != [item['key'], item['url']]:
            raise ValueError('ENS stored source identity changed')
        return digest(['stored', values])

    def _ens_page(self, page):
        url = self.LIST_URL + '?page=' + str(page)
        raw = self._curl_get(url, context=f'backfill list page {page}')
        if not raw:
            raise ConnectionError('ENS listing unavailable')
        if access_challenge(raw):
            raise PermissionError('ENS access challenge')
        soup = self._make_soup(raw, context=f'backfill list page {page}')
        view = soup.select_one('.view-communiques-de-presse') if soup else None
        if view is None or self._node_text(soup.select_one('h1')) != 'Communiqués de presse':
            raise ValueError('ENS press catalogue markup missing')
        items = []
        for article in view.select('article'):
            key = (article.get('data-history-node-id') or '').strip()
            link = article.select_one('a[href]')
            href = (link.get('href') or '').strip() if link else ''
            target = urlparse(urljoin(self.base_url, href))
            if (not re.fullmatch(r'\d+', key) or not href or target.scheme != 'https'
                    or target.netloc != urlparse(self.base_url).netloc or target.path == '/'):
                raise ValueError('ENS invalid source card')
            items.append(dict(key=key, url=urlunparse(target._replace(query='', fragment='')),
                              source_text=self._node_text(article)))
        if len({i['key'] for i in items}) != len(items) or len({i['url'] for i in items}) != len(items):
            raise ValueError('ENS repeated card within one source page')
        links = view.select('.pager__item--next a, a[rel="next"]')
        if links and {urljoin(url, a.get('href', '')) for a in links} != {self.LIST_URL + '?page=' + str(page + 1)}:
            raise ValueError('ENS non-adjacent or foreign paginator')
        if not items and links:
            raise ValueError('ENS empty page still advertises a next page')
        return dict(items=items, has_next=bool(links), page_hash=digest([items, bool(links)]))

    def _ens_excluded_prefix(self, item, expected):
        if self._ens_row(item, required=False) is not None:
            raise ValueError('ENS excluded source now has a stored row')
        raw = self._curl_get(item['url'], context='verify excluded source body')
        if not raw:
            raise ConnectionError('ENS excluded source unavailable')
        paper = self._parse_article(item['key'], item['url'], raw)
        if paper is not None or self.delivery_source_failure or self._ens_last_exclusion != expected:
            raise ValueError('ENS exclusion evidence changed')
        return digest(['excluded_short_body', item['key'], item['url'], expected])

    def _crawl_ens_backfill(self, limit):
        self._check_cancelled()
        if limit is not None and limit <= 0:
            return 0
        self._source_failure, self._exhausted, self._pending_cursor = None, False, None
        self._cursor_items_done = 0
        resume = self.delivery_cursor
        excluded = {}
        if resume is not None:
            try:
                assert isinstance(resume, dict) and resume.get('version') == 1
                assert resume.get('contract') == self._ens_contract()
                assert type(resume.get('page')) is int and resume['page'] >= 0
                assert type(resume.get('offset')) is int and resume['offset'] >= 0
                assert type(resume.get('page_items')) is int and resume['page_items'] > 0
                assert resume['offset'] <= resume['page_items']
                assert all(isinstance(resume.get(k), str) and re.fullmatch('[0-9a-f]{64}', resume[k])
                           for k in ('page_hash', 'prefix_hash', 'rows_hash'))
                excluded = json.loads(resume['exclusions_json'])
                assert isinstance(excluded, dict)
                assert all(re.fullmatch(r'\d+', k) and isinstance(v, str) and re.fullmatch('[0-9a-f]{64}', v)
                           for k, v in excluded.items())
            except (AssertionError, KeyError, TypeError, ValueError):
                self._report_source_failure('configuration')
                return 0
        saved, page = 0, 0
        prefix, rows_hash = digest(['ens-source-prefix']), digest(['ens-stored-prefix'])
        reached, started = resume is None, time.monotonic()
        seen_keys, seen_urls, verified_excluded = {}, {}, {}
        prior_has_next = None

        def budget():
            if time.monotonic() - started < self.WALL_CLOCK_LIMIT:
                return False
            if not self.delivery_cursor_items_done:
                self._report_source_failure('invalid_response')
            return True

        def remember(item):
            key, url = item['key'], item['url']
            if key in seen_keys or url in seen_urls:
                if seen_keys.get(key) != url or seen_urls.get(url) != key:
                    raise ValueError('ENS source key or URL reused inconsistently')
                return False
            seen_keys[key], seen_urls[url] = url, key
            return True

        def checkpoint(data, offset, done=0):
            self._advance_cursor(dict(version=1, contract=self._ens_contract(), page=page,
                                      offset=offset, page_items=len(data['items']),
                                      page_hash=data['page_hash'], prefix_hash=prefix, rows_hash=rows_hash,
                                      exclusions_json=json.dumps(verified_excluded, sort_keys=True)), items_done=done)

        while True:
            self._check_cancelled()
            if limit is not None and saved >= limit or budget():
                return saved
            if page >= self.MAX_PAGES:
                self._report_source_failure('invalid_response')
                return saved
            try:
                data = self._ens_page(page)
                if not data['items']:
                    if reached and prior_has_next is not True:
                        self._mark_exhausted()
                    else:
                        self._report_source_failure('invalid_response')
                    return saved
                if prior_has_next is False:
                    raise ValueError('ENS records beyond advertised final page')
                if all(i['key'] in seen_keys for i in data['items']):
                    raise ValueError('ENS source page repeats an earlier page')
                at_cursor = bool(resume and page == resume['page'])
                if at_cursor and (data['page_hash'] != resume['page_hash']
                                  or len(data['items']) != resume['page_items']
                                  or prefix != resume['prefix_hash']):
                    raise ValueError('ENS source prefix changed')
                offset = resume['offset'] if at_cursor else 0
                if not reached:
                    for item in data['items'][:offset] if at_cursor else data['items']:
                        self._check_cancelled()
                        if budget():
                            return saved
                        if not remember(item):
                            row = digest(['duplicate_membership', item['key'], item['url']])
                        elif item['key'] in excluded:
                            fingerprint = excluded[item['key']]
                            row = self._ens_excluded_prefix(item, fingerprint)
                            verified_excluded[item['key']] = fingerprint
                        else:
                            row = self._ens_row(item, required=True)
                        rows_hash = digest([rows_hash, row])
                    if at_cursor:
                        if rows_hash != resume['rows_hash'] or verified_excluded != excluded:
                            raise ValueError('ENS stored or excluded prefix changed')
                        reached = True
                if reached:
                    checkpoint(data, offset)
                    for index, item in enumerate(data['items'][offset:], offset):
                        self._check_cancelled()
                        if limit is not None and saved >= limit or budget():
                            return saved
                        new = remember(item)
                        if not new:
                            row = digest(['duplicate_membership', item['key'], item['url']])
                        else:
                            try:
                                previous = self._ens_row(item, required=False)
                                self._last_processed_seq_id = None
                                self._ens_last_exclusion = None
                                created = self._process_article(item['key'], item['url'])
                                if self.delivery_source_failure:
                                    return saved
                                if self._last_processed_seq_id is not None:
                                    saved += int(bool(created))
                                    row = self._ens_row(item, required=True)
                                elif self._ens_last_exclusion and previous is None:
                                    verified_excluded[item['key']] = self._ens_last_exclusion
                                    row = digest(['excluded_short_body', item['key'], item['url'], self._ens_last_exclusion])
                                else:
                                    raise ValueError('ENS detail not stored or explicitly excluded')
                            except Exception:
                                self._report_source_failure('item_failed')
                                return saved
                        rows_hash = digest([rows_hash, row])
                        checkpoint(data, index + 1, done=int(new))
                        self._check_cancelled()
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
            prefix = digest([prefix, page, data['page_hash']])
            prior_has_next = data['has_next']
            page += 1
