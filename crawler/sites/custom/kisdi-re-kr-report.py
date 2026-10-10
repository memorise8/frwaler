# -*- coding: utf-8 -*-
"""KISDI's three delivered publication scopes, with guarded backfill cursors.

Source artId values remain unchanged. Scope/master conflicts are held for review
instead of merging documents or changing historical identifiers. A contents list
alone does not establish that the publication's abstract/body was collected.
"""
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from requests import RequestException

from crawler.base_crawler import BaseCrawler
from crawler.source_cursor_prefix import prefix_seed, advance_prefix, verify_source_prefix

_BASE = 'https://www.kisdi.re.kr'
_LIST_URL = _BASE + '/report/list.do'
_DETAIL_URL = _BASE + '/report/view.do'
_FILE_DOWN = _BASE + '/report/fileDown.do'
_SCOPES = {
    'basic': ('m2101113024153', '3934560'),
    'premium': ('m2101113025536', '3934550'),
    'journal': ('m2309145696871', '5850223'),
}
_WALL_SECONDS = int(os.environ.get('LIBERTREE_MAX_WALL_S', str(25 * 60)))
_PDF_MAX_BYTES = 100 * 1024**2


def _make_soup(html):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, 'html.parser')


def _text(node):
    return re.sub(r'\s+', ' ', node.get_text(' ', strip=True)).strip() if node else ''


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _list_url(scope, page):
    key, master = _SCOPES[scope]
    return _LIST_URL + '?' + urlencode(dict(key=key, arrMasterId=master, pageIndex=page))


def _detail_url(scope, art_id):
    key, master = _SCOPES[scope]
    return _DETAIL_URL + '?' + urlencode(dict(key=key, masterId=master, arrMasterId=master, artId=art_id))


def _listing(html, scope, page):
    soup = _make_soup(html)
    key, master = _SCOPES[scope]
    form = soup.select_one('form#frm')
    if not form or not soup.select_one('ul#dataList'):
        raise ValueError('missing source listing structure')
    master_input = form.select_one('input[name="arrMasterId"]')
    per_page = form.select_one('input[name="recordCountPerPage"]')
    if not master_input or master_input.get('value') != master or not per_page:
        raise ValueError('source scope mismatch')
    size = int(per_page.get('value', ''))
    total = int(_text(soup.select_one('#pageCurrEIndex')).replace(',', ''))
    current = int(_text(soup.select_one('#pageCurrSIndex')))
    last = int(_text(soup.select_one('#pageMaxIndex')))
    if size <= 0 or total < 0:
        raise ValueError('invalid source count')
    if total == 0:
        if page != 1 or current not in (0, 1) or last not in (0, 1):
            raise ValueError('invalid empty source page')
    elif current != page or not 1 <= page <= last or last != (total + size - 1) // size:
        raise ValueError('source pagination mismatch')
    items = []
    for card in soup.select('ul#dataList > li'):
        anchors = card.select('a[onclick]')
        targets = [(a, re.fullmatch(r"\s*goView\('([0-9]+)',\s*'([0-9]+)'\);?\s*", a['onclick'])) for a in anchors]
        targets = [(a, match) for a, match in targets if match]
        if len(targets) != 1:
            raise ValueError('missing or ambiguous source document target')
        anchor, match = targets[0]
        if match[1] != master:
            raise ValueError('cross-master listing target')
        art_id = match[2]
        title = _text(anchor.find('strong', recursive=False))
        if not title:
            raise ValueError('source document title missing')
        date_raw = ''
        for li in anchor.select('li'):
            label = _text(li.find('strong'))
            if label == '발행일':
                date_raw = _text(li)[len(label):].strip()
        downloads = set()
        for a in card.select('a.download[href]'):
            url = urljoin(_BASE, a['href'])
            parsed = urlsplit(url)
            if (parsed.scheme != 'https' or parsed.netloc != 'www.kisdi.re.kr'
                    or parsed.path != '/report/fileDown.do' or parsed.fragment
                    or parse_qs(parsed.query) != dict(key=[key], arrMasterId=[master], id=[art_id])):
                raise ValueError('unreviewed source download identity')
            downloads.add(url)
        if len(downloads) > 1:
            raise ValueError('multiple primary download references')
        items.append(dict(art_id=art_id, master_id=master, title=title, published_date_raw=date_raw,
                          url=_detail_url(scope, art_id), pdf_url=next(iter(downloads), None)))
    expected = min(size, total - (page - 1) * size) if total else 0
    if len(items) != expected or len({x['art_id'] for x in items}) != len(items):
        raise ValueError('source listing count or identity mismatch')
    return dict(items=items, total=total, last_page=last, page_size=size,
                page_hash=_hash([(r['master_id'], r['art_id'], r['title'], r['published_date_raw'], r['pdf_url']) for r in items]))


class KisdiReKrReportCrawler(BaseCrawler):
    site_id = 'kisdi-re-kr-report'
    site_name = 'Custom: kisdi-re-kr-report'
    base_url = _BASE
    DELIVERY_CURSOR_VERSION = 1

    def _curl(self, method, url, post_fields=None):
        cmd = ['curl', '--tls-max', '1.3', '-sS', '--max-time', '30', '-X', method,
               '-H', f'User-Agent: {self.USER_AGENT}', '-H', 'Referer: ' + _LIST_URL,
               '--write-out', '\n%{http_code}']
        if post_fields:
            for key, value in post_fields.items():
                cmd += ['--data-urlencode', f'{key}={value}']
        cmd.append(url)
        for attempt in range(3):
            self._check_cancelled()
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                self._check_cancelled()
                raw, separator, code = result.stdout.rpartition(b'\n')
                status = int(code) if separator and code.isdigit() else 0
                if status in (401, 403, 429):
                    raise PermissionError(f'HTTP {status}')
                # Service failures are not authentication failures. Do not send
                # retries while a busy publisher may be asking for a cooldown.
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
                time.sleep(3 * (attempt + 1))
        return None

    def _fetch_list(self, page, scope='basic'):
        raw = self._curl('GET', _list_url(scope, page))
        if not raw:
            raise ConnectionError('source listing unavailable')
        return raw

    def _fetch_detail(self, art_id, scope='basic'):
        raw = self._curl('POST', _detail_url(scope, art_id), {'pageIndex': '1'})
        if not raw:
            raise ConnectionError('source detail unavailable')
        return raw

    def _parse_detail(self, html, item):
        soup = _make_soup(html)
        title = _text(soup.find('h4', id='h4Title'))
        if not title or title != item['title']:
            raise ValueError('source detail/list identity mismatch')
        values = {}
        for li in soup.select('ul#data > li'):
            label = _text(li.find('strong'))
            if label:
                values[label] = _text(li)[len(label):].strip()
        abstract = re.sub(r'^요약\s*', '', _text(soup.find(id='tab1'))).strip()
        toc = re.sub(r'^목차\s*', '', _text(soup.find(id='tab2'))).strip()
        provenance = {}
        if len(abstract) < 100 and item.get('pdf_url'):
            abstract, provenance = self._abstract_from_pdf(item)
        if len(abstract) < 100:
            raise ValueError('source body unavailable; contents alone is insufficient')
        if toc:
            abstract += '\n\n[목차] ' + toc
        return dict(site_id=self.site_id, external_id=item['art_id'], title=title, abstract=abstract,
                    authors=[v.strip() for v in re.split(r'[,，]', values.get('저자', '')) if v.strip()],
                    published_date=values.get('발행일') or item['published_date_raw'] or None,
                    posted_date=item['published_date_raw'] or None, url=item['url'], pdf_url=item['pdf_url'],
                    category=values.get('분류정보', ''), keywords=', '.join(_text(s) for s in soup.select('div.tag span')),
                    metadata=json.dumps(dict(master_id=item['master_id'], journal=values.get('게재지', ''),
                                             volume=values.get('권호', ''), toc=toc, **provenance), ensure_ascii=False))

    def _abstract_from_pdf(self, item):
        """Recover an empty HTML summary from its source-linked report PDF.

        Keep artId/master identity and extraction provenance; persistent file
        artifacts and their flags remain the separate file worker's concern.
        """
        from delivery.files.worker import run_stage

        scope = next((name for name, (_, master) in _SCOPES.items() if master == item['master_id']), None)
        uid = item['art_id']
        if scope is None or not re.fullmatch(r'[0-9]+', uid) or item['url'] != _detail_url(scope, uid):
            raise ValueError('unreviewed source PDF document identity')
        pdf_url = item['pdf_url']
        parsed = urlsplit(pdf_url)
        key, master = _SCOPES[scope]
        if (parsed.scheme != 'https' or parsed.netloc != 'www.kisdi.re.kr'
                or parsed.path != '/report/fileDown.do' or parsed.fragment
                or parse_qs(parsed.query, keep_blank_values=True) != dict(key=[key], arrMasterId=[master], id=[uid])):
            raise ValueError('source PDF download identity mismatch')
        self._check_cancelled()
        started, size, digest = time.monotonic(), 0, hashlib.sha256()
        with tempfile.TemporaryDirectory(prefix='kisdi-source-pdf-') as directory:
            root = Path(directory)
            with self._session.get(pdf_url, stream=True, timeout=(10, 45), allow_redirects=False,
                                   headers={'Referer': item['url']}) as response:
                if response.status_code in (401, 403, 429):
                    raise PermissionError(f'HTTP {response.status_code}')
                if response.status_code != 200:
                    raise ConnectionError(f'source PDF HTTP {response.status_code}')
                declared = int(response.headers.get('Content-Length', '0'))
                if declared < 0 or declared > _PDF_MAX_BYTES:
                    raise ValueError('source PDF exceeds size budget')
                with (root / 'source.pdf').open('xb') as handle:
                    for chunk in response.iter_content(65536):
                        self._check_cancelled()
                        size += len(chunk)
                        if size > _PDF_MAX_BYTES or time.monotonic() - started > 240:
                            raise ValueError('source PDF download budget exceeded')
                        digest.update(chunk)
                        handle.write(chunk)
            if declared and declared != size:
                raise ValueError('source PDF truncated or length changed')
            with (root / 'source.pdf').open('rb') as handle:
                if handle.read(5) != b'%PDF-':
                    raise ValueError('source response is not PDF')
            self._check_cancelled()
            stage = run_stage('text', root, timeout=120)
            self._check_cancelled()
            if not stage.get('success'):
                raise ValueError('source PDF text unavailable: ' + str(stage.get('error', 'unknown')))
            with (root / 'output.txt').open(encoding='utf-8') as handle:
                excerpt = ' '.join(handle.read(12000).split())[:3000]
            with (root / 'output.txt').open('rb') as handle:
                text_hash = hashlib.file_digest(handle, 'sha256').hexdigest()
        if len(excerpt) < 100:
            raise ValueError('source PDF excerpt below body threshold')
        return excerpt, dict(abstract_source='publisher_pdf_text_excerpt', abstract_pdf_url=pdf_url,
                             abstract_pdf_sha256=digest.hexdigest(), abstract_pdf_bytes=size,
                             abstract_pdf_text_sha256=text_hash, abstract_pdf_text_characters=stage.get('chars'))

    def _assert_identity(self, item):
        postgres = os.environ.get('LIBERTREE_DB_BACKEND', 'sqlite').lower() == 'postgres'
        if not postgres and not self._conn_is_libertree():
            return
        mark = '%s' if postgres else '?'
        rows = self._conn.execute(f'SELECT post_number,meta_url FROM documents WHERE site_id={mark} '
                                  f'AND (post_number={mark} OR meta_url={mark})',
                                  (self.site_id, item['art_id'], item['url'])).fetchall()
        if len(rows) > 1:
            raise ValueError('ambiguous stored source identity')
        for row in rows:
            post, url = (row['post_number'], row['meta_url']) if hasattr(row, 'keys') else row
            if post != item['art_id'] or url != item['url']:
                raise ValueError('stored source identity requires review')

    @staticmethod
    def _contract():
        return _hash(_SCOPES)

    def _validate_cursor(self, cursor):
        if (not isinstance(cursor, dict) or cursor.get('version') != self.DELIVERY_CURSOR_VERSION
                or cursor.get('contract') != self._contract() or cursor.get('scope') not in _SCOPES):
            raise ValueError('invalid cursor contract')
        for key in ('page', 'offset', 'total', 'last_page', 'page_size'):
            if type(cursor.get(key)) is not int or cursor[key] < (0 if key == 'offset' else 1):
                raise ValueError('invalid cursor range')
        if (cursor['page'] > cursor['last_page']
                or cursor['last_page'] != (cursor['total'] + cursor['page_size'] - 1) // cursor['page_size']
                or cursor['offset'] > min(cursor['page_size'], cursor['total'] - (cursor['page'] - 1) * cursor['page_size'])):
            raise ValueError('invalid cursor page or offset')
        if not re.fullmatch('[0-9a-f]{64}', cursor.get('page_hash', '')):
            raise ValueError('invalid cursor anchor')
        if 'prefix_hash' in cursor and not re.fullmatch('[0-9a-f]{64}', str(cursor['prefix_hash'])):
            raise ValueError('invalid source prefix anchor')
        if cursor['page'] == 1:
            if cursor.get('previous_hash') != '':
                raise ValueError('unexpected previous-page anchor')
        elif not re.fullmatch('[0-9a-f]{64}', cursor.get('previous_hash', '')):
            raise ValueError('missing previous-page anchor')

    def _checkpoint(self, scope, page, offset, data, previous_hash, done=0):
        if self._cursor_enabled:
            self._advance_cursor(dict(version=self.DELIVERY_CURSOR_VERSION, contract=self._contract(), scope=scope,
                                      page=page, offset=offset, total=data['total'], last_page=data['last_page'],
                                      page_size=data['page_size'], page_hash=data['page_hash'], previous_hash=previous_hash,
                                      prefix_hash=self._cursor_prefix_hash),
                                 items_done=done)

    def _failure(self, exc, item=False):
        if isinstance(exc, PermissionError):
            reason = 'rate_limited' if '429' in str(exc) else 'authentication_failed'
        elif isinstance(exc, (ConnectionError, RequestException)):
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
        self._cursor_prefix_hash = prefix_seed(self._contract())
        if resume:
            try:
                self._cursor_prefix_hash, seen = verify_source_prefix(
                    self, resume, _SCOPES, first_page=1,
                    load_page=lambda scope, page: _listing(self._fetch_list(page, scope), scope, page),
                    bounds=lambda data: (data['total'], data['last_page'], data['page_size']), identity_key='art_id',
                    date_key='published_date_raw', deadline=started + _WALL_SECONDS)
            except Exception as exc:
                self._failure(exc)
                return 0
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
            page, offset = (current['page'], current['offset']) if current else (1, 0)
            previous = current['previous_hash'] if current else ''
            bounds = tuple(current[k] for k in ('total', 'last_page', 'page_size')) if current else None
            while True:
                self._check_cancelled()
                if limit is not None and saved >= limit or budget_hit():
                    return saved
                try:
                    if current and page > 1:
                        anchor = _listing(self._fetch_list(page - 1, scope), scope, page - 1)
                        if anchor['page_hash'] != previous or tuple(anchor[k] for k in ('total', 'last_page', 'page_size')) != bounds:
                            raise ValueError('previous source page changed')
                    data = _listing(self._fetch_list(page, scope), scope, page)
                    actual_bounds = tuple(data[k] for k in ('total', 'last_page', 'page_size'))
                    if bounds is not None and actual_bounds != bounds:
                        raise ValueError('source pagination changed')
                    if current and data['page_hash'] != current['page_hash']:
                        raise ValueError('current source page changed')
                    if page > 1 and data['page_hash'] == previous:
                        raise ValueError('source page repeated')
                    bounds = actual_bounds
                    if data['total'] == 0:
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
                        if item['art_id'] in seen:
                            raise ValueError('cross-scope source identity collision')
                        self._assert_identity(item)
                        time.sleep(self._delay)
                        self._check_cancelled()
                        paper = self._parse_detail(self._fetch_detail(item['art_id'], scope), item)
                        self._save_paper(paper)
                        if self._last_save_created:
                            saved += 1
                        seen[item['art_id']] = item['url']
                        self._checkpoint(scope, page, index + 1, data, previous, done=1)
                        self._check_cancelled()
                    except Exception as exc:
                        self._failure(exc, item=True)
                        if self._cursor_enabled or isinstance(exc, (PermissionError, ConnectionError, RequestException)):
                            return saved
                if self._cursor_enabled:
                    self._cursor_prefix_hash = advance_prefix(
                        self._cursor_prefix_hash, scope, page, data['page_hash'], (data['total'], data['last_page'], data['page_size']))
                if page == data['last_page']:
                    break
                previous, page, offset, current = data['page_hash'], page + 1, 0, None
        self._check_cancelled()
        if not self.delivery_source_failure:
            self._mark_exhausted()
        return saved
