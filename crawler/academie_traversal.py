"""Anchor the complete Académie press scope, stored rows, and unreadable source bodies."""
import hashlib
import json
import os
import re
import time

from delivery.health.access_challenge import access_challenge


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), default=str).encode()).hexdigest()


class AcademieTraversal:
    DELIVERY_CURSOR_VERSION = 1
    DELIVERY_STOP_ON_KNOWN = False

    def _ac_contract(self):
        return digest(['academie-all-press-sections-v1', self._AC_START_URL,
                       self.MIN_ABSTRACT_CHARS, 'decoded_source_path_identity', 'printed_PDF_date_roles_v1',
                       'source_body_exclusions_revalidated_by_bytes_and_text'])

    def _ac_row(self, item, *, required):
        postgres = os.environ.get('LIBERTREE_DB_BACKEND', 'sqlite').lower() == 'postgres'
        if not postgres and not self._conn_is_libertree():
            raise ValueError('Academie cursor requires the delivery database')
        fields = ('seq_id', 'post_number', 'meta_url', 'title', 'abstract', 'pdf_url',
                  'original_filename', 'authors', 'publisher', 'journal', 'keywords',
                  'published_date', 'published_date_raw', 'published_date_status',
                  'published_date_precision', 'listed_date', 'listed_date_raw',
                  'listed_date_status', 'listed_date_precision')
        mark = '%s' if postgres else '?'
        rows = self._conn.execute(f'SELECT {",".join(fields)} FROM documents '
                                  f'WHERE site_id={mark} AND meta_url={mark}',
                                  (self.site_id, item['url'])).fetchall()
        if not rows and not required:
            return None
        if len(rows) != 1:
            raise ValueError('Academie stored source identity missing or ambiguous')
        values = [rows[0][k] for k in fields] if hasattr(rows[0], 'keys') else list(rows[0])
        if values[1:3] != [item['key'], item['url']]:
            raise ValueError('Academie stored source identity changed')
        return digest(['stored', values])

    def _ac_page(self, page):
        url = self._list_url(page)
        raw = self._fetch_text(url, accept='text/html,*/*', context=f'list page {page+1}')
        if not raw:
            raise ConnectionError('Academie source catalogue unavailable')
        if access_challenge(raw):
            raise PermissionError('Academie source access challenge')
        if not re.search(r'<html\b', raw, re.I) or not re.search(r'</html\s*>', raw, re.I):
            raise ValueError('Academie incomplete source catalogue')
        soup = self._ac_soup(raw)
        if soup is None:
            raise ValueError('Academie source markup unavailable')
        native, next_page = self._parse_list_items(soup, page+1, url)
        items = []
        for item in native:
            target = item.get('detail_url') or item['pdf_url']
            items.append(dict(key=self._ac_external_id(target), url=target, native=item))
        if len({item['key'] for item in items}) != len(items):
            raise ValueError('Academie repeated source identity within one page')
        if not items and next_page:
            raise ValueError('Academie empty page still advertises another page')
        return dict(items=items, has_next=bool(next_page), page_hash=digest([items, bool(next_page)]))

    @staticmethod
    def _ac_membership(item):
        native = {k:v for k,v in item['native'].items() if k not in ('list_page', 'list_url')}
        return digest([item['key'], item['url'], native])

    def _ac_exclude_body(self, item, raw, text, reason):
        self._ac_last_exclusion = digest([
            'source_body_unavailable', item.get('detail_url') or item['pdf_url'],
            item.get('title'), hashlib.sha256(raw).hexdigest(), text, self.MIN_ABSTRACT_CHARS, reason,
        ])
        self._ac_last_exclusion_reason = reason
        return None

    def _ac_process(self, item):
        return self._process_item(item['native'])

    def _ac_excluded_prefix(self, item, expected):
        if self._ac_row(item, required=False) is not None:
            raise ValueError('Academie excluded source now has a stored row')
        paper = self._prepare_item(item['native'])
        if paper is not None or self._ac_last_exclusion != expected:
            raise ValueError('Academie excluded source body or bytes changed')
        return digest(['excluded_source_body', item['key'], item['url'], expected])

    def _crawl_ac(self, limit):
        self._check_cancelled()
        if limit is not None and limit <= 0:
            return 0
        self._source_failure, self._exhausted, self._pending_cursor = None, False, None
        self._cursor_items_done = 0
        resume = self.delivery_cursor if self.delivery_mode == 'backfill' else None
        excluded = {}
        if resume is not None:
            try:
                assert isinstance(resume, dict) and resume.get('version') == 1
                assert resume.get('contract') == self._ac_contract()
                assert type(resume.get('page')) is int and resume['page'] >= 0
                assert type(resume.get('offset')) is int and resume['offset'] >= 0
                assert type(resume.get('page_items')) is int and resume['page_items'] > 0
                assert resume['offset'] <= resume['page_items']
                assert all(isinstance(resume.get(k), str) and re.fullmatch('[0-9a-f]{64}', resume[k])
                           for k in ('page_hash', 'prefix_hash', 'rows_hash'))
                excluded = json.loads(resume['exclusions_json'])
                assert isinstance(excluded, dict)
                assert all(isinstance(k, str) and bool(k) and isinstance(v, str) and re.fullmatch('[0-9a-f]{64}', v)
                           for k, v in excluded.items())
            except (AssertionError, KeyError, TypeError, ValueError):
                self._report_source_failure('configuration')
                return 0
        saved, page = 0, 0
        prefix, rows_hash = digest(['academie-source-prefix']), digest(['academie-stored-prefix'])
        reached, started = resume is None, time.monotonic()
        seen_keys, seen_urls, verified_excluded = {}, {}, {}
        seen_items = {}
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
                if (seen_keys.get(key) != url or seen_urls.get(url) != key
                        or seen_items.get(key) != self._ac_membership(item)):
                    raise ValueError('Academie source key or URL reused inconsistently')
                return False
            seen_keys[key], seen_urls[url] = url, key
            seen_items[key] = self._ac_membership(item)
            return True

        def checkpoint(data, offset, done=0):
            self._advance_cursor(dict(version=1, contract=self._ac_contract(), page=page,
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
                data = self._ac_page(page)
                if not data['items']:
                    if reached and prior_has_next is not True:
                        self._mark_exhausted()
                    else:
                        self._report_source_failure('invalid_response')
                    return saved
                if prior_has_next is False:
                    raise ValueError('Academie records beyond advertised final page')
                if all(i['key'] in seen_keys for i in data['items']):
                    raise ValueError('Academie source page repeats an earlier page')
                at_cursor = bool(resume and page == resume['page'])
                if at_cursor and (data['page_hash'] != resume['page_hash']
                                  or len(data['items']) != resume['page_items']
                                  or prefix != resume['prefix_hash']):
                    raise ValueError('Academie source prefix changed')
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
                            row = self._ac_excluded_prefix(item, fingerprint)
                            verified_excluded[item['key']] = fingerprint
                        else:
                            row = self._ac_row(item, required=True)
                        rows_hash = digest([rows_hash, row])
                    if at_cursor:
                        if rows_hash != resume['rows_hash'] or verified_excluded != excluded:
                            raise ValueError('Academie stored or excluded prefix changed')
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
                                previous = self._ac_row(item, required=False)
                                self._last_processed_seq_id = None
                                self._ac_last_exclusion = None
                                created = self._ac_process(item)
                                if self.delivery_source_failure:
                                    return saved
                                if self._last_processed_seq_id is not None:
                                    saved += int(bool(created))
                                    row = self._ac_row(item, required=True)
                                elif self._ac_last_exclusion and previous is None:
                                    verified_excluded[item['key']] = self._ac_last_exclusion
                                    row = digest(['excluded_source_body', item['key'], item['url'], self._ac_last_exclusion])
                                else:
                                    raise ValueError('Academie detail not stored or explicitly excluded')
                            except Exception:
                                self._report_source_failure('item_failed')
                                return saved
                        rows_hash = digest([rows_hash, row])
                        checkpoint(data, index + 1, done=int(new))
                        self._check_cancelled()
                        time.sleep(max(0, self.detail_delay))
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
