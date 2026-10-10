"""Traverse all delivered KEI scopes with source and stored-prefix anchors."""
import hashlib
import json
import os
import re
import time
from urllib.parse import urlencode
from requests.exceptions import RequestException

from crawler.document_identity import find_kei_document
from crawler.kei_details import parse_detail
from crawler.kei_source import BASE, PAGE_SIZE, SCOPES, parse_listing, title_quality
from crawler.source_cursor_prefix import advance_prefix, prefix_seed


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), default=str).encode()).hexdigest()


class KeiTraversal:
    DELIVERY_CURSOR_VERSION = 1
    DELIVERY_STOP_ON_KNOWN = False

    def _scope_contract(self):
        return digest(['kei-scoped-source-and-stored-prefix-v1', list(SCOPES.items()),
                       self._MIN_ABSTRACT])

    def _scope_page(self, scope, page):
        key = scope, page
        if key not in self._scope_pages:
            if page == 1:
                path, mid, name = SCOPES[scope]
                selector = 'elibName' if path == '/elibList.es' else 'bid'
                url = BASE + path + '?' + urlencode({'mid': mid, selector: name})
            else:
                url = self._scope_pages[(scope, page - 1)]['next_url']
            if not url:
                raise ValueError('KEI adjacent source page unavailable')
            raw = self._curl_get(url, referer=BASE)
            if not raw:
                raise ConnectionError('KEI source listing unavailable')
            self._scope_pages[key] = dict(parse_listing(raw, scope, page), url=url)
        return self._scope_pages[key]

    def _scope_detail(self, item, listing_url):
        raw = self._curl_get(item['url'], referer=listing_url)
        if not raw:
            raise ConnectionError('KEI source detail unavailable')
        return parse_detail(raw, item, allow_pdf_recovery=True)

    def _scope_paper(self, item, detail):
        if item.get('title_quality') or title_quality(detail['title']):
            raise ValueError('KEI source title quality requires review')
        abstract, provenance = detail['abstract'], {}
        malformed = detail.get('body_classification') == 'malformed_source_HTML_requires_PDF'
        if malformed and not detail['primary_PDF_candidate']:
            raise ValueError('KEI malformed source body requires validated PDF recovery')
        if (malformed or len(abstract) < self._MIN_ABSTRACT) and detail['primary_PDF_candidate']:
            from crawler.kei_pdf import source_pdf_excerpt
            abstract, provenance = source_pdf_excerpt(self, item, detail)
        if len(abstract) < self._MIN_ABSTRACT:
            raise ValueError('KEI source body below accepted text threshold')
        pdf = detail['primary_PDF_candidate']
        return dict(site_id=self.site_id, external_id=item['source_key'],
                    title=detail['title'], abstract=abstract,
                    published_date=detail['published_date_raw'],
                    listed_date=detail['listed_date_raw'], authors=detail['authors'],
                    publisher='한국환경연구원', url=item['url'],
                    pdf_url=pdf['url'] if pdf else None,
                    original_filename=pdf['filename'] if pdf else None,
                    category=item.get('category', ''),
                    metadata=json.dumps(dict(detail, **provenance), ensure_ascii=False))

    def _stored_scope_digest(self, item):
        postgres = os.environ.get('LIBERTREE_DB_BACKEND', 'sqlite').lower() == 'postgres'
        if not postgres and not self._conn_is_libertree():
            raise ValueError('KEI durable cursor requires the delivery database')
        seq = find_kei_document(self._conn, self.site_id, item['source_key'], item['url'],
                                postgres=postgres)
        if seq is None:
            raise ValueError('KEI cursor prefix document missing')
        # File/translation processing can change independently. Anchor the
        # persisted source fields and original identity without rewriting them.
        fields = ('seq_id', 'post_number', 'meta_url', 'title', 'abstract', 'pdf_url',
                  'published_date', 'published_date_raw', 'published_date_status',
                  'published_date_precision', 'listed_date', 'listed_date_raw',
                  'listed_date_status', 'listed_date_precision')
        mark = '%s' if postgres else '?'
        row = self._conn.execute(f'SELECT {",".join(fields)} FROM documents WHERE seq_id={mark}',
                                 (seq,)).fetchone()
        values = [row[k] for k in fields] if hasattr(row, 'keys') else list(row)
        return digest([item['source_key'], values])

    def _validate_scope_cursor(self, c):
        if (not isinstance(c, dict) or c.get('version') != self.DELIVERY_CURSOR_VERSION
                or c.get('contract') != self._scope_contract() or c.get('scope') not in SCOPES):
            raise ValueError('KEI cursor contract mismatch')
        for key in ('page', 'last_page', 'total', 'offset'):
            if type(c.get(key)) is not int or c[key] < (0 if key in ('total', 'offset') else 1):
                raise ValueError('KEI cursor range invalid')
        if (c['last_page'] != max(1, (c['total'] + PAGE_SIZE - 1) // PAGE_SIZE)
                or c['page'] > c['last_page']
                or c['offset'] > min(PAGE_SIZE, c['total'] - (c['page'] - 1) * PAGE_SIZE)):
            raise ValueError('KEI cursor bounds invalid')
        for key in ('page_hash', 'prefix_hash', 'rows_hash'):
            if not isinstance(c.get(key), str) or not re.fullmatch('[0-9a-f]{64}', c[key]):
                raise ValueError('KEI cursor anchor missing')
        if c['page'] == 1:
            if c.get('previous_hash') != '':
                raise ValueError('KEI unexpected previous page anchor')
        elif not isinstance(c.get('previous_hash'), str) or not re.fullmatch('[0-9a-f]{64}', c['previous_hash']):
            raise ValueError('KEI previous page anchor missing')

    def _scope_failure(self, exc, item=False):
        if isinstance(exc, PermissionError):
            self._report_source_failure('rate_limited' if any(n in str(exc) for n in ('429', '503'))
                                        else 'authentication_failed')
            return
        self._report_source_failure('fetch_failed' if isinstance(exc, (ConnectionError, RequestException))
                                    else 'item_failed' if item else 'invalid_response')

    def _crawl_scopes(self, limit=None):
        self._check_cancelled()
        if limit is not None and limit <= 0:
            return 0
        self._source_failure, self._exhausted, self._pending_cursor = None, False, None
        self._cursor_items_done, self._scope_pages, self._access_held = 0, {}, False
        enabled = self.delivery_mode == 'backfill'
        resume = self.delivery_cursor if enabled else None
        if resume is not None:
            try:
                self._validate_scope_cursor(resume)
            except Exception:
                self._report_source_failure('configuration')
                return 0
        started, saved, seen = time.monotonic(), 0, set()
        prefix, rows_hash = prefix_seed(self._scope_contract()), digest(['kei-stored-prefix'])
        reached = resume is None
        pages_read = 0

        def budget():
            if time.monotonic() - started <= self._TIMEOUT_SECS:
                return False
            if not enabled or not self.delivery_cursor_items_done:
                self._report_source_failure('invalid_response')
            return True

        def checkpoint(scope, page, offset, data, previous, done=0):
            if enabled:
                self._advance_cursor(dict(version=self.DELIVERY_CURSOR_VERSION,
                                          contract=self._scope_contract(), scope=scope,
                                          page=page, offset=offset, total=data['total'],
                                          last_page=data['last_page'], page_hash=data['page_hash'],
                                          previous_hash=previous, prefix_hash=prefix,
                                          rows_hash=rows_hash), items_done=done)

        for scope in SCOPES:
            page, previous, bounds = 1, '', None
            while True:
                self._check_cancelled()
                if (limit is not None and saved >= limit) or budget():
                    return saved
                if self._MAX_PAGES > 0 and pages_read >= self._MAX_PAGES:
                    self._report_source_failure('invalid_response')
                    return saved
                try:
                    data = self._scope_page(scope, page)
                    pages_read += 1
                    actual = data['total'], data['last_page']
                    if bounds is not None and actual != bounds:
                        raise ValueError('KEI scope bounds changed')
                    bounds = actual
                    at_cursor = bool(resume and scope == resume['scope'] and page == resume['page'])
                    if at_cursor:
                        if (bounds != (resume['total'], resume['last_page'])
                                or data['page_hash'] != resume['page_hash']
                                or previous != resume['previous_hash']
                                or prefix != resume['prefix_hash']):
                            raise ValueError('KEI source prefix changed')
                    offset = resume['offset'] if at_cursor else 0
                    if not reached:
                        for item in data['items'][:offset] if at_cursor else data['items']:
                            self._check_cancelled()
                            if budget():
                                return saved
                            if item['source_key'] in seen:
                                raise ValueError('KEI source prefix repeats identity')
                            seen.add(item['source_key'])
                            rows_hash = digest([rows_hash, self._stored_scope_digest(item)])
                        if at_cursor:
                            if rows_hash != resume['rows_hash']:
                                raise ValueError('KEI stored source prefix changed')
                            reached = True
                    if reached:
                        checkpoint(scope, page, offset, data, previous)
                except Exception as exc:
                    self._scope_failure(exc)
                    return saved
                if reached:
                    for index, item in enumerate(data['items'][offset:], offset):
                        self._check_cancelled()
                        if (limit is not None and saved >= limit) or budget():
                            return saved
                        if item['source_key'] in seen:
                            self._report_source_failure('invalid_response')
                            return saved
                        seen.add(item['source_key'])
                        try:
                            time.sleep(self._RATE_SLEEP)
                            self._check_cancelled()
                            detail = self._scope_detail(item, data['url'])
                            self._save_paper(self._scope_paper(item, detail))
                            if self._last_save_created:
                                saved += 1
                            if enabled:
                                rows_hash = digest([rows_hash, self._stored_scope_digest(item)])
                            checkpoint(scope, page, index + 1, data, previous, done=1)
                            self._check_cancelled()
                        except Exception as exc:
                            self._scope_failure(exc, item=True)
                            if enabled or isinstance(exc, (ConnectionError, PermissionError, RequestException)) or self._access_held:
                                return saved
                prefix = advance_prefix(prefix, scope, page, data['page_hash'], bounds)
                if page == data['last_page']:
                    break
                previous, page = data['page_hash'], page + 1
        if not reached:
            self._report_source_failure('invalid_response')
        elif not self.delivery_source_failure:
            self._mark_exhausted()
        return saved
