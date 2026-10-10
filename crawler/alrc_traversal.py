"""Source-anchored traversal of both delivered ALRC lists."""
import re
import time

from crawler.alrc_scope import SCOPES, list_url, page_identity, scope_contract


class AlrcTraversal:
    DELIVERY_CURSOR_VERSION = 1
    DELIVERY_ORDER = 'arbitrary'

    def _alrc_validate_cursor(self, cursor):
        if (not isinstance(cursor, dict) or type(cursor.get('version')) is not int
                or cursor.get('version') != self.DELIVERY_CURSOR_VERSION
                or cursor.get('contract') != scope_contract() or cursor.get('scope') not in SCOPES):
            raise ValueError('ALRC cursor contract changed')
        for name in ('page', 'last_page', 'rows'):
            if type(cursor.get(name)) is not int or cursor[name] < 1:
                raise ValueError('ALRC invalid cursor position')
        if (cursor['page'] > cursor['last_page'] or type(cursor.get('offset')) is not int
                or not 0 <= cursor['offset'] <= cursor['rows']):
            raise ValueError('ALRC invalid row offset')
        for name, required in (('page_hash', True), ('previous_hash', cursor['page'] > 1)):
            value = cursor.get(name)
            if not isinstance(value, str) or (required and not re.fullmatch('[0-9a-f]{64}', value)):
                raise ValueError('ALRC invalid page anchor')
        if cursor['page'] == 1 and cursor['previous_hash'] != '':
            raise ValueError('ALRC unexpected previous page')

    def _alrc_checkpoint(self, scope, page, offset, last, items, previous_hash, done=0):
        if self._alrc_cursor_enabled:
            self._advance_cursor(dict(version=self.DELIVERY_CURSOR_VERSION, contract=scope_contract(),
                                      scope=scope, page=page, offset=offset, last_page=last,
                                      rows=len(items), page_hash=page_identity(items),
                                      previous_hash=previous_hash), items_done=done)

    def crawl(self, limit=None):
        self._check_cancelled()
        if limit is not None and limit <= 0:
            return 0
        self._alrc_cursor_enabled = self.delivery_mode == 'backfill'
        resume = self.delivery_cursor if self._alrc_cursor_enabled else None
        if resume is not None:
            try:
                self._alrc_validate_cursor(resume)
            except (ValueError, TypeError):
                self._report_source_failure('configuration')
                return 0
        saved, seen_urls = 0, set()
        started = time.monotonic()
        self.delivery_body_gaps = []

        def budget():
            self._check_cancelled()
            if time.monotonic() - started < self._alrc_wall_seconds():
                return False
            if not self._alrc_cursor_enabled or self.delivery_cursor_items_done == 0:
                self._report_source_failure('invalid_response')
            return True

        for scope in SCOPES[SCOPES.index(resume['scope']) if resume else 0:]:
            current = resume if resume and resume['scope'] == scope else None
            page = current['page'] if current else 1
            offset = current['offset'] if current else 0
            boundary = current['last_page'] if current else None
            previous_hash = current['previous_hash'] if current else ''
            seen_page_hashes = set()
            if current and page > 1:
                if budget():
                    return saved
                try:
                    prior, last = self._alrc_list(scope, page - 1)
                    if last != boundary or page_identity(prior) != previous_hash:
                        raise ValueError('ALRC previous source page changed')
                except PermissionError as exc:
                    self._report_source_failure('rate_limited' if '429' in str(exc) else 'authentication_failed')
                    return saved
                except Exception:
                    self._report_source_failure('invalid_response')
                    return saved
            while True:
                if limit is not None and saved >= limit:
                    return saved
                if budget():
                    return saved
                try:
                    items, last = self._alrc_list(scope, page)
                    identity = page_identity(items)
                    if boundary is None:
                        boundary = last
                    if last != boundary or identity in seen_page_hashes or identity == previous_hash:
                        raise ValueError('ALRC source pages changed or repeated')
                    if current and (identity != current['page_hash'] or len(items) != current['rows']):
                        raise ValueError('ALRC current source page changed')
                    if offset > len(items):
                        raise ValueError('ALRC source page shorter than offset')
                    seen_page_hashes.add(identity)
                except PermissionError as exc:
                    self._report_source_failure('rate_limited' if '429' in str(exc) else 'authentication_failed')
                    return saved
                except Exception:
                    self._report_source_failure('invalid_response')
                    return saved
                self._alrc_checkpoint(scope, page, offset, boundary, items, previous_hash)
                for index, item in enumerate(items):
                    if index < offset:
                        continue
                    if limit is not None and saved >= limit:
                        return saved
                    if budget():
                        return saved
                    if item['url'] in seen_urls:
                        self._alrc_checkpoint(scope, page, index + 1, boundary, items, previous_hash, done=1)
                        continue
                    try:
                        paper = self._alrc_paper(item, scope, list_url(scope, page))
                        self._check_cancelled()
                        if paper is None:
                            self.delivery_body_gaps.append(dict(scope=scope, url=item['url']))
                            raise ValueError('ALRC source body unavailable')
                        if self._save_paper(paper) is None:
                            raise ValueError('ALRC document was not accepted by storage')
                        if self._last_save_created:
                            saved += 1
                        seen_urls.add(item['url'])
                        self._alrc_checkpoint(scope, page, index + 1, boundary, items, previous_hash, done=1)
                        self._check_cancelled()
                    except PermissionError as exc:
                        self._report_source_failure('rate_limited' if '429' in str(exc) else 'authentication_failed')
                        return saved
                    except Exception:
                        self._report_source_failure('item_failed')
                        if self._alrc_cursor_enabled:
                            return saved
                if page == boundary:
                    break
                previous_hash = identity
                page += 1
                offset = 0
                current = None
        if not self.delivery_source_failure:
            self._mark_exhausted()
        return saved
