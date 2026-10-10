"""Resume the UN Geneva press catalogue with source, row and exclusion anchors."""
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


class UNGenevaTraversal:
    DELIVERY_CURSOR_VERSION = 1
    DELIVERY_STOP_ON_KNOWN = False

    def _ung_contract(self):
        return digest(['ungeneva-press-v1', self._LIST_URL, self.MIN_ABSTRACT_CHARS,
                       'delivered_URL_identity_with_reused_slugs', 'short_body_exclusion_revalidated'])

    def _ung_row(self, item, *, required):
        postgres = os.environ.get('LIBERTREE_DB_BACKEND', 'sqlite').lower() == 'postgres'
        if not postgres and not self._conn_is_libertree():
            raise ValueError('UN Geneva cursor requires the delivery database')
        fields = ('seq_id', 'post_number', 'meta_url', 'title', 'abstract', 'pdf_url',
                  'original_filename', 'authors', 'publisher', 'journal', 'keywords',
                  'published_date', 'published_date_raw',
                  'published_date_status', 'published_date_precision', 'listed_date',
                  'listed_date_raw', 'listed_date_status', 'listed_date_precision')
        mark = '%s' if postgres else '?'
        rows = self._conn.execute(f'SELECT {",".join(fields)} FROM documents '
                                  f'WHERE site_id={mark} AND meta_url={mark}',
                                  (self.site_id, item['url'])).fetchall()
        if not rows and not required:
            return None
        if len(rows) != 1:
            raise ValueError('UN Geneva stored source identity missing or ambiguous')
        values = [rows[0][k] for k in fields] if hasattr(rows[0], 'keys') else list(rows[0])
        if values[1:3] != [item['slug'], item['url']]:
            raise ValueError('UN Geneva stored source identity changed')
        return digest(['stored', values])

    @staticmethod
    def _ung_text(node):
        return ' '.join(node.get_text(' ', strip=True).split()) if node else ''

    def _ung_document(self, raw):
        if not raw:
            raise ConnectionError('UN Geneva source unavailable')
        if access_challenge(raw):
            raise PermissionError('UN Geneva access challenge')
        if not re.search(r'<html\b', raw, re.I) or not re.search(r'</html\s*>', raw, re.I):
            raise ValueError('UN Geneva source HTML document incomplete')
        soup = self._ung_soup(raw)
        if soup is None:
            raise ValueError('UN Geneva source markup unavailable')
        return soup

    def _ung_page(self, page):
        url = self._LIST_URL + '?page=' + str(page)
        soup = self._ung_document(self._ung_fetch(url))
        view = soup.select_one('.news-listing')
        if view is None or self._ung_text(soup.select_one('h1')) != 'Press Releases':
            raise ValueError('UN Geneva press catalogue markup missing')
        items = []
        for card in view.select('div.views-row a.un-listings-box'):
            target = urlparse(urljoin(url, card.get('href', '')))
            title = self._ung_text(card.select_one('h2.item-title'))
            date = card.select_one('time[datetime]')
            published = date.get('datetime', '') if date else ''
            if (target.scheme != 'https' or target.netloc != urlparse(self.base_url).netloc
                    or target.query or target.fragment or not title or not published
                    or not re.fullmatch(r'/en/news-media/press-release/\d{4}/(?:0[1-9]|1[0-2])/[^/?#]+/?', target.path)):
                raise ValueError('UN Geneva invalid source card')
            destination = urlunparse(target)
            items.append(dict(key=destination, url=destination, slug=target.path.rstrip('/').split('/')[-1],
                              title=title, listed_date=published, source_text=self._ung_text(card)))
        if len({i['key'] for i in items}) != len(items):
            raise ValueError('UN Geneva repeated card within one source page')
        links = view.select('.pager__item--next a, a[rel="next"]')
        if links and {urljoin(url, a.get('href', '')) for a in links} != {self._LIST_URL + '?page=' + str(page + 1)}:
            raise ValueError('UN Geneva non-adjacent or foreign paginator')
        if not items and links:
            raise ValueError('UN Geneva empty page still advertises a next page')
        return dict(items=items, has_next=bool(links), page_hash=digest([items, bool(links)]))

    def _ung_detail(self, item):
        raw = self._ung_fetch(item['url'])
        soup = self._ung_document(raw)
        canonical = soup.select_one('link[rel=canonical]')
        target = canonical.get('href', '') if canonical else ''
        if target != item['url']:
            # The delivered /en/ catalogue contains a French article which
            # explicitly declares the same dated path as its French alternate.
            expected = self.base_url + '/fr/' + urlparse(item['url']).path[4:]
            alternates = {a.get('href') for a in soup.select('link[rel=alternate][hreflang=fr]')}
            if target != expected or target not in alternates:
                raise ValueError('UN Geneva source canonical identity mismatch')
        date = soup.select_one('time[datetime]')
        body = soup.select_one('div.text-long')
        if (self._ung_text(soup.select_one('h1')) != item['title']
                or date is None or date.get('datetime') != item['listed_date'] or body is None):
            raise ValueError('UN Geneva source detail identity or body shape mismatch')
        paper = self._parse_article(item['url'], item['slug'], item['listed_date'], raw)
        if paper is not None:
            if (' '.join(paper['title'].split()) != item['title']
                    or paper['published_date'] != item['listed_date']):
                raise ValueError('UN Geneva parsed source fields mismatch')
            return paper, None
        description = soup.find('meta', property='og:description')
        fingerprint = digest(['excluded_short_body', item['key'], item['slug'], item['title'],
                              item['listed_date'], self._ung_text(body),
                              description.get('content', '') if description else '', self.MIN_ABSTRACT_CHARS])
        return None, fingerprint

    def _ung_process(self, item):
        paper, exclusion = self._ung_detail(item)
        self._ung_last_exclusion = exclusion
        if paper is None:
            return False
        self._last_processed_seq_id = self._save_paper(paper)
        return self._last_save_created

    def _ung_excluded_prefix(self, item, expected):
        if self._ung_row(item, required=False) is not None:
            raise ValueError('UN Geneva excluded source now has a stored row')
        paper, fingerprint = self._ung_detail(item)
        if paper is not None or fingerprint != expected:
            raise ValueError('UN Geneva exclusion evidence changed')
        return digest(['excluded_short_body', item['key'], item['url'], expected])

    def _crawl_ung_backfill(self, limit):
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
                assert resume.get('contract') == self._ung_contract()
                assert type(resume.get('page')) is int and resume['page'] >= 0
                assert type(resume.get('offset')) is int and resume['offset'] >= 0
                assert type(resume.get('page_items')) is int and resume['page_items'] > 0
                assert resume['offset'] <= resume['page_items']
                assert all(isinstance(resume.get(k), str) and re.fullmatch('[0-9a-f]{64}', resume[k])
                           for k in ('page_hash', 'prefix_hash', 'rows_hash'))
                excluded = json.loads(resume['exclusions_json'])
                assert isinstance(excluded, dict)
                assert all(isinstance(k, str) and k.startswith(self._LIST_URL + '/') and isinstance(v, str) and re.fullmatch('[0-9a-f]{64}', v)
                           for k, v in excluded.items())
            except (AssertionError, KeyError, TypeError, ValueError):
                self._report_source_failure('configuration')
                return 0
        saved, page = 0, 0
        prefix, rows_hash = digest(['ungeneva-source-prefix']), digest(['ungeneva-stored-prefix'])
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
                        or seen_items.get(key) != digest(item)):
                    raise ValueError('UN Geneva source key or URL reused inconsistently')
                return False
            seen_keys[key], seen_urls[url] = url, key
            seen_items[key] = digest(item)
            return True

        def checkpoint(data, offset, done=0):
            self._advance_cursor(dict(version=1, contract=self._ung_contract(), page=page,
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
                data = self._ung_page(page)
                if not data['items']:
                    if reached and prior_has_next is not True:
                        self._mark_exhausted()
                    else:
                        self._report_source_failure('invalid_response')
                    return saved
                if prior_has_next is False:
                    raise ValueError('UN Geneva records beyond advertised final page')
                if all(i['key'] in seen_keys for i in data['items']):
                    raise ValueError('UN Geneva source page repeats an earlier page')
                at_cursor = bool(resume and page == resume['page'])
                if at_cursor and (data['page_hash'] != resume['page_hash']
                                  or len(data['items']) != resume['page_items']
                                  or prefix != resume['prefix_hash']):
                    raise ValueError('UN Geneva source prefix changed')
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
                            row = self._ung_excluded_prefix(item, fingerprint)
                            verified_excluded[item['key']] = fingerprint
                        else:
                            row = self._ung_row(item, required=True)
                        rows_hash = digest([rows_hash, row])
                    if at_cursor:
                        if rows_hash != resume['rows_hash'] or verified_excluded != excluded:
                            raise ValueError('UN Geneva stored or excluded prefix changed')
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
                                previous = self._ung_row(item, required=False)
                                self._last_processed_seq_id = None
                                self._ung_last_exclusion = None
                                created = self._ung_process(item)
                                if self.delivery_source_failure:
                                    return saved
                                if self._last_processed_seq_id is not None:
                                    saved += int(bool(created))
                                    row = self._ung_row(item, required=True)
                                elif self._ung_last_exclusion and previous is None:
                                    verified_excluded[item['key']] = self._ung_last_exclusion
                                    row = digest(['excluded_short_body', item['key'], item['url'], self._ung_last_exclusion])
                                else:
                                    raise ValueError('UN Geneva detail not stored or explicitly excluded')
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
