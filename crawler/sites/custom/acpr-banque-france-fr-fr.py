# -*- coding: utf-8 -*-
"""ACPR delivered filtered studies and press releases with durable resume.

Keep the two delivered study format filters on every pagination request. Card
slugs and existing document identities are preserved; collisions need review.
"""
import hashlib
import json
import os
import re
import subprocess
import time
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit

from crawler.base_crawler import BaseCrawler

_BASE = 'https://acpr.banque-france.fr'
_STUDY_QUERY = dict(search_publications_api_fulltext='', **{
    'format[5412591]': '5412591', 'format[5412600]': '5412600',
    'start-date_year': '', 'start-date_month': '', 'end-date_year': '', 'end-date_month': '',
})
_SCOPES = {
    'studies': dict(path='/fr/publications-et-statistiques/etudes-et-recherche', query=_STUDY_QUERY,
                    detail='/fr/publications-et-statistiques/publications/',
                    view='.view-id-espace2_liste_des_publications_et_statistique'),
    'press': dict(path='/fr/communiques-de-presse', query={}, detail='/fr/communiques-de-presse/',
                  view='.view-id-espaces2_press_release'),
}
_WALL_SECONDS = int(os.environ.get('LIBERTREE_MAX_WALL_S', str(25 * 60)))


def _make_soup(html):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, 'html.parser')


def _text(node):
    return ' '.join(node.get_text(' ', strip=True).split()) if node else ''


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _list_url(scope, page):
    cfg = _SCOPES[scope]
    return _BASE + cfg['path'] + '?' + urlencode(dict(cfg['query'], page=page))


def _page_number(url, scope):
    cfg, parsed = _SCOPES[scope], urlsplit(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    pages = query.pop('page', [])
    if (parsed.scheme != 'https' or parsed.netloc != 'acpr.banque-france.fr'
            or parsed.path != cfg['path'] or parsed.fragment or len(pages) != 1
            or not re.fullmatch(r'[0-9]+', pages[0])
            or query != {k: [v] for k, v in cfg['query'].items()}):
        raise ValueError('source pagination changed delivered filters or destination')
    return int(pages[0])


def _listing(html, scope, page):
    cfg = _SCOPES[scope]
    soup = _make_soup(html)
    root = soup.select_one(cfg['view'])
    if not root:
        raise ValueError('missing delivered listing view')
    items = []
    for card in root.select('a.card-vertical'):
        url = urljoin(_BASE, card.get('href', ''))
        parsed = urlsplit(url)
        title = _text(card.select_one('.card-title'))
        if (parsed.scheme != 'https' or parsed.netloc != 'acpr.banque-france.fr'
                or not parsed.path.startswith(cfg['detail']) or parsed.path == cfg['detail']
                or parsed.query or parsed.fragment or not title):
            raise ValueError('unreviewed source document target')
        dates = [_text(x) for x in card.select('small') if re.search(r'\d{4}', _text(x)) and len(_text(x)) < 60]
        if len(set(dates)) > 1:
            raise ValueError('ambiguous source card date')
        items.append(dict(url=url, slug=parsed.path.rstrip('/').rsplit('/', 1)[-1], title=title,
                          listed_date_raw=dates[0] if dates else '', category=_text(card.select_one('.category-btn-grid'))))
    if len({x['url'] for x in items}) != len(items):
        raise ValueError('duplicate source card target')
    pager = root.select_one('nav.pager')
    if not items:
        if page != 0 or pager or not _text(root.select_one('.view-empty')):
            raise ValueError('unexplained empty source page')
        return dict(items=[], last_page=0, next_url=None, page_hash=_hash([]))
    if not pager:
        raise ValueError('missing source pagination proof')
    numbers = [_page_number(urljoin(_list_url(scope, page), a['href']), scope) for a in pager.select('a[href]')]
    active = pager.select('a[aria-current="page"]')
    if len(active) != 1 or _page_number(urljoin(_list_url(scope, page), active[0]['href']), scope) != page:
        raise ValueError('source current page mismatch')
    last = max(numbers)
    # The publisher marks its previous arrow rel="next" too. Validate the
    # destinations, accepting that adjacent previous link without following it.
    directional = [(a, _page_number(urljoin(_list_url(scope, page), a['href']), scope))
                   for a in pager.select('a[rel~="next"]')]
    if any(number not in (page - 1, page + 1) for _, number in directional):
        raise ValueError('source pagination skips or repeats a page')
    following = [a for a, number in directional if number == page + 1]
    if len(following) != (1 if page < last else 0):
        raise ValueError('missing or unexpected source next page')
    next_url = urljoin(_list_url(scope, page), following[0]['href']) if following else None
    if next_url and _page_number(next_url, scope) != page + 1:
        raise ValueError('source pagination skips or repeats a page')
    return dict(items=items, last_page=last, next_url=next_url,
                page_hash=_hash([(x['url'], x['title'], x['listed_date_raw']) for x in items]))


class ACPRBanqueFranceCrawler(BaseCrawler):
    site_id = 'acpr-banque-france-fr-fr'
    site_name = 'Custom: acpr-banque-france-fr-fr'
    base_url = _BASE
    DELIVERY_CURSOR_VERSION = 1

    def _curl_get(self, url):
        cmd = ['curl', '--tls-max', '1.3', '-sSL', '--max-time', '30',
               '-H', 'Accept: text/html,application/xhtml+xml,*/*;q=0.8',
               '-H', 'Accept-Language: fr-FR,fr;q=0.9,en;q=0.8', '-H', f'User-Agent: {self.USER_AGENT}',
               '--write-out', '\n%{http_code}', url]
        for attempt in range(3):
            self._check_cancelled()
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                self._check_cancelled()
                raw, separator, code = result.stdout.rpartition(b'\n')
                status = int(code) if separator and code.isdigit() else 0
                if status in (401, 403, 429):
                    raise PermissionError(f'HTTP {status}')
                if status == 503:
                    raise ConnectionError('HTTP 503')
                from delivery.health.access_challenge import access_challenge
                if access_challenge(raw):
                    raise PermissionError('source access challenge')
                if result.returncode == 0 and status == 200 and raw:
                    return raw.decode('utf-8', errors='replace')
                if status and status < 500:
                    return None
            except (PermissionError, ConnectionError):
                raise
            except (OSError, subprocess.TimeoutExpired):
                pass
            if attempt < 2:
                self._check_cancelled()
                time.sleep(3 ** attempt)
        return None

    def _html(self, url):
        self._check_cancelled()
        raw = self._curl_get(url)
        if not raw:
            raise ConnectionError('source page unavailable')
        return raw

    def _parse_detail(self, html, item):
        soup = _make_soup(html)
        main = soup.select_one('article.node') or soup.find('main')
        if not main or _text(soup.find('h1')) != item['title']:
            raise ValueError('source detail/list identity mismatch')
        # Never use menus, contact footers or unrelated paragraphs to fill gaps.
        fields = main.select('.field--name-field-contenu-riche,.field-contenu-riche,.field--name-body,.field--type-text-long')
        fields += main.select('.field--name-field-espaces2-header-text,.field-espaces2-description,.field--name-field-espaces2-description,.field--name-field-description')
        parts = []
        for field in fields:
            for node in field.select('script,style,nav,footer'):
                node.decompose()
            text = _text(field)
            if text and text not in parts:
                parts.append(text)
        abstract = '\n\n'.join(parts)
        if len(abstract) < 50:
            raise ValueError('source body unavailable')
        dates = set()
        for script in soup.select('script[data-drupal-selector="drupal-settings-json"]'):
            data = json.loads(script.string or script.get_text())
            raw = data.get('piano_props', {}).get('custom_publication_statistic_date')
            if raw:
                if not isinstance(raw, str):
                    raise ValueError('unreviewed publication date type')
                dates.add(raw)
        if len(dates) > 1:
            raise ValueError('conflicting source publication dates')
        # Retain the original value; ambiguous numeric dates need a separately
        # reviewed site date-order rule, rather than silent string truncation.
        date_raw = next(iter(dates), item['listed_date_raw'])
        pdfs = []
        for a in main.select('a[href]'):
            url = urljoin(_BASE, a['href'])
            if a.get('data-file-extension', '').lower() == 'pdf' or urlsplit(url).path.lower().endswith('.pdf'):
                if urlsplit(url).scheme != 'https':
                    raise ValueError('unreviewed document file scheme')
                if url not in pdfs:
                    pdfs.append(url)
        pdf_url = pdfs[0] if pdfs else None
        return dict(site_id=self.site_id, external_id=item['slug'], title=item['title'], abstract=abstract,
                    published_date=date_raw or None, posted_date=item['listed_date_raw'] or None,
                    url=item['url'], pdf_url=pdf_url,
                    original_filename=unquote(urlsplit(pdf_url).path.rsplit('/', 1)[-1]) if pdf_url else None,
                    authors=[], publisher='ACPR - Autorité de contrôle prudentiel et de résolution',
                    category=item['category'], metadata=json.dumps(dict(source_pdf_urls=pdfs), ensure_ascii=False))

    def _assert_identity(self, item):
        postgres = os.environ.get('LIBERTREE_DB_BACKEND', 'sqlite').lower() == 'postgres'
        if not postgres and not self._conn_is_libertree():
            return
        mark = '%s' if postgres else '?'
        rows = self._conn.execute(f'SELECT post_number,meta_url FROM documents WHERE site_id={mark} '
                                  f'AND (post_number={mark} OR meta_url={mark})',
                                  (self.site_id, item['slug'], item['url'])).fetchall()
        if len(rows) > 1:
            raise ValueError('ambiguous stored source identity')
        for row in rows:
            post, url = (row['post_number'], row['meta_url']) if hasattr(row, 'keys') else row
            if post != item['slug'] or url != item['url']:
                raise ValueError('stored source identity requires review')

    @staticmethod
    def _contract():
        return _hash(_SCOPES)

    def _validate_cursor(self, cursor):
        if (not isinstance(cursor, dict) or cursor.get('version') != self.DELIVERY_CURSOR_VERSION
                or cursor.get('contract') != self._contract() or cursor.get('scope') not in _SCOPES):
            raise ValueError('invalid cursor contract')
        for key in ('page', 'offset', 'last_page', 'page_items'):
            if type(cursor.get(key)) is not int or cursor[key] < (1 if key == 'page_items' else 0):
                raise ValueError('invalid cursor range')
        if cursor['page'] > cursor['last_page'] or cursor['offset'] > cursor['page_items']:
            raise ValueError('invalid cursor position')
        if not re.fullmatch('[0-9a-f]{64}', cursor.get('page_hash', '')):
            raise ValueError('invalid source page anchor')
        if cursor['page'] == 0:
            if cursor.get('previous_hash') != '':
                raise ValueError('unexpected previous-page anchor')
        elif not re.fullmatch('[0-9a-f]{64}', cursor.get('previous_hash', '')):
            raise ValueError('missing previous-page anchor')

    def _checkpoint(self, scope, page, offset, data, previous_hash, done=0):
        if self._cursor_enabled:
            self._advance_cursor(dict(version=self.DELIVERY_CURSOR_VERSION, contract=self._contract(), scope=scope,
                                      page=page, offset=offset, last_page=data['last_page'], page_items=len(data['items']),
                                      page_hash=data['page_hash'], previous_hash=previous_hash), items_done=done)

    def _failure(self, exc, item=False):
        if isinstance(exc, PermissionError):
            reason = 'rate_limited' if '429' in str(exc) else 'authentication_failed'
        elif isinstance(exc, ConnectionError):
            reason = 'fetch_failed'
        else:
            reason = 'item_failed' if item else 'invalid_response'
        self._report_source_failure(reason)

    def crawl(self, limit=None):
        self._check_cancelled()
        if limit is not None and limit <= 0:
            return 0
        self._source_failure, self._exhausted, self._pending_cursor, self._cursor_items_done = None, False, None, 0
        self._cursor_enabled = self.delivery_mode == 'backfill'
        resume = self.delivery_cursor if self._cursor_enabled else None
        if resume is not None:
            try:
                self._validate_cursor(resume)
            except Exception:
                self._report_source_failure('configuration')
                return 0
        started, saved, seen = time.monotonic(), 0, {}
        def budget_hit():
            if time.monotonic() - started <= _WALL_SECONDS:
                return False
            if not self._cursor_enabled or self.delivery_cursor_items_done == 0:
                self._report_source_failure('invalid_response')
            return True
        for scope in _SCOPES:
            if resume and list(_SCOPES).index(scope) < list(_SCOPES).index(resume['scope']):
                continue
            current = resume if resume and scope == resume['scope'] else None
            page, offset = (current['page'], current['offset']) if current else (0, 0)
            previous = current['previous_hash'] if current else ''
            last = current['last_page'] if current else None
            url = _list_url(scope, page)
            while True:
                self._check_cancelled()
                if limit is not None and saved >= limit or budget_hit():
                    return saved
                try:
                    if current and page > 0:
                        anchor = _listing(self._html(_list_url(scope, page - 1)), scope, page - 1)
                        if anchor['page_hash'] != previous or anchor['last_page'] != last:
                            raise ValueError('previous source page changed')
                    data = _listing(self._html(url), scope, page)
                    if last is not None and data['last_page'] != last:
                        raise ValueError('source pagination changed')
                    if current and (data['page_hash'] != current['page_hash'] or len(data['items']) != current['page_items']):
                        raise ValueError('current source page changed')
                    if page > 0 and data['page_hash'] == previous:
                        raise ValueError('source page repeated')
                    last = data['last_page']
                    if not data['items']:
                        break
                except Exception as exc:
                    self._failure(exc)
                    return saved
                self._checkpoint(scope, page, offset, data, previous)
                for index, item in enumerate(data['items']):
                    if index < offset:
                        continue
                    self._check_cancelled()
                    if limit is not None and saved >= limit or budget_hit():
                        return saved
                    try:
                        if item['slug'] in seen and seen[item['slug']] != item['url']:
                            raise ValueError('source slug collision')
                        self._assert_identity(item)
                        time.sleep(self._delay)
                        self._check_cancelled()
                        paper = self._parse_detail(self._html(item['url']), item)
                        self._save_paper(paper)
                        if self._last_save_created:
                            saved += 1
                        seen[item['slug']] = item['url']
                        self._checkpoint(scope, page, index + 1, data, previous, done=1)
                        self._check_cancelled()
                    except Exception as exc:
                        self._failure(exc, item=True)
                        if self._cursor_enabled or isinstance(exc, (PermissionError, ConnectionError)):
                            return saved
                if page == last:
                    break
                previous, page, offset, current, url = data['page_hash'], page + 1, 0, None, data['next_url']
        self._check_cancelled()
        if not self.delivery_source_failure:
            self._mark_exhausted()
        return saved
