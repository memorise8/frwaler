# -*- coding: utf-8 -*-
"""ADR UK's delivered impact, publications and annual-report metadata scopes.

Annual index destinations are reconciled with listed annual publications. Their
own report PDFs are primary files; linked HTML chapters are not extra records.
File-worker/full-content validation remains separate from metadata exhaustion.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

from crawler.base_crawler import BaseCrawler

_SITE_ID = 'adruk-org-news-publications'
_BASE_URL = 'https://www.adruk.org'
_PUBLISHER = 'ADR UK'
_ANNUAL_INDEX = _BASE_URL + '/news-publications/annual-reports/'
_SCOPES = {
    'impact': dict(entry='/news-publications/impact-case-studies/', pages='/our-mission/our-impact/',
                   detail='/our-mission/our-impact/article/', model='model-impact-cases'),
    'publications': dict(entry='/news-publications/publications-reports/', pages='/news-publications/publications-reports/',
                         detail='/news-publications/publications-reports/publication/', model='model-publications-amp-amp-reports'),
}
_WALL_CLOCK_LIMIT = int(os.environ.get('LIBERTREE_MAX_WALL_S', str(25 * 60)))
_PDF_MAX_BYTES = 200 * 1024 * 1024


def _make_soup(html):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, 'html.parser')


def _text(node):
    return node.get_text(' ', strip=True) if node else ''


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _curl_get(url, *, timeout=30, retries=3, check_cancelled=None):
    cmd = ['curl', '-sSL', '--tls-max', '1.3', '--max-time', str(timeout),
           '-H', 'User-Agent: Mozilla/5.0', '-H', 'Accept: text/html,application/xhtml+xml,*/*;q=0.8',
           '--write-out', '\n%{http_code}', url]
    for attempt in range(retries):
        if check_cancelled:
            check_cancelled()
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            if check_cancelled:
                check_cancelled()
            body, separator, raw_status = result.stdout.rpartition(b'\n')
            status = int(raw_status) if separator and raw_status.isdigit() else 0
            if status in (401, 403, 429, 503):
                raise PermissionError(f'HTTP {status}')
            if result.returncode == 0 and status == 200 and body:
                from delivery.health.access_challenge import access_challenge
                if access_challenge(body):
                    raise PermissionError('source access challenge')
                return body.decode('utf-8', errors='replace')
            if status and status < 500:
                return None
        except PermissionError:
            raise
        except (subprocess.TimeoutExpired, OSError):
            pass
        if attempt < retries - 1:
            if check_cancelled:
                check_cancelled()
            time.sleep(3 ** attempt)
    return None


def _list_url(scope, page):
    cfg = _SCOPES[scope]
    return _BASE_URL + (cfg['entry'] if page == 0 else cfg['pages'] + str(page) + '/')


def _parse_listing(html, scope='impact'):
    cfg = _SCOPES[scope]
    records = []
    for box in _make_soup(html).select('div.record.' + cfg['model']):
        anchor = box.find('a', href=True)
        if not anchor:
            raise ValueError('listing record without a target')
        url = urljoin(_BASE_URL, anchor['href'])
        parsed = urlsplit(url)
        direct_pdf = scope == 'publications' and parsed.path.startswith('/fileadmin/') and parsed.path.lower().endswith('.pdf')
        if (parsed.scheme != 'https' or parsed.netloc != 'www.adruk.org' or parsed.query or parsed.fragment
                or not (parsed.path.startswith(cfg['detail']) or direct_pdf)):
            raise ValueError('unreviewed listing target')
        title = _text(box.find('h2'))
        if not title:
            raise ValueError('listing record without title')
        records.append(dict(url=url, slug=parsed.path.rstrip('/').rsplit('/', 1)[-1],
                            list_title=title, listed_date_raw=_text(box.select_one('span.date')),
                            list_abstract=' '.join(_text(p) for p in box.find_all('p') if 'fauxLink' not in (p.get('class') or [])),
                            direct_pdf=direct_pdf))
    return records


def _listing(html, scope, page):
    soup = _make_soup(html)
    matches = set(re.findall(r'Displaying results (\d+) to (\d+) out of (\d+)', _text(soup)))
    if len(matches) != 1:
        raise ValueError('missing or ambiguous listing range')
    first, last, total = map(int, matches.pop())
    items = _parse_listing(html, scope)
    if total == 0:
        if (first, last, page, len(items)) != (0, 0, 0, 0):
            raise ValueError('invalid empty listing')
    elif (first < 1 or last < first or last > total or len(items) != last - first + 1
          or page == 0 and first != 1 or len({x['url'] for x in items}) != len(items)):
        raise ValueError('listing range does not match records')
    nexts = {urljoin(_BASE_URL, a['href']) for a in soup.find_all('a', href=True) if _text(a) == 'Next »'}
    if nexts != ({_list_url(scope, page + 1)} if last < total else set()):
        raise ValueError('missing, repeated or unexpected next link')
    return dict(items=items, first=first, last=last, total=total,
                page_hash=_hash([(i['url'], i['list_title'], i['listed_date_raw']) for i in items]))


def _poster_collection_pdfs(content):
    """Poster title links belong to the named collection, without a primary."""
    links, active_level = [], None
    for node in content.find_all(['h2', 'h3', 'h4', 'p']):
        if node.name.startswith('h'):
            level = int(node.name[1])
            if _text(node).casefold() == 'poster presentations':
                active_level = level
            elif active_level is not None and level <= active_level:
                active_level = None
        elif active_level is not None:
            for anchor in node.select('strong a[href], b a[href]'):
                if _text(anchor) and urlsplit(anchor['href']).path.lower().endswith('.pdf'):
                    links.append(urljoin(_BASE_URL, anchor['href']))
    return list(dict.fromkeys(links))


def _parse_detail(html):
    soup = _make_soup(html)
    main = soup.select_one('.news.single.model-impact-cases,.news.single.model-publications-amp-amp-reports')
    content = main.select_one('.content') if main else None
    if not main or not content or not soup.find('h1'):
        raise ValueError('missing document body structure')
    title = _text(soup.find('h1'))
    date_raw = _text(main.select_one('span.date'))
    categories = [_text(a) for a in main.select('p.category a')]
    pdfs = list(dict.fromkeys(urljoin(_BASE_URL, a['href']) for a in main.find_all('a', href=True)
                              if urlsplit(a['href']).path.lower().endswith('.pdf')))
    # ADR also publishes report buttons without a separate download list.
    # Prefer the explicit download list for the primary URL, then retain every
    # report-button variant. Ordinary narrative links still require review.
    downloads = list(dict.fromkeys(
        urljoin(_BASE_URL, a['href']) for a in main.select('li.download a[href]')
        if urlsplit(a['href']).path.lower().endswith('.pdf')))
    buttons = list(dict.fromkeys(
        urljoin(_BASE_URL, a['href']) for a in content.select('p.button a[href]')
        if urlsplit(a['href']).path.lower().endswith('.pdf')))
    primary_files = list(dict.fromkeys(downloads + buttons))
    collection_files = _poster_collection_pdfs(content)
    attachments = list(dict.fromkeys(primary_files + collection_files))
    report_links = sorted({urldefrag(urljoin(_BASE_URL, a['href']))[0] for a in content.find_all('a', href=True)
                           if urlsplit(urljoin(_BASE_URL, a['href'])).netloc == 'reports.adruk.org'})
    authors, parts = [], []
    for node in content.select('script,style,nav,.image'):
        node.decompose()
    for node in content.find_all(['p', 'h2', 'h3', 'h4', 'li']):
        if node.find_parent(['p', 'li']):
            continue
        text = _text(node)
        if not text:
            continue
        if re.match(r'Authors?\s*:', text, re.I):
            authors = [s.strip() for s in re.split(r'\s+and\s+|;\s*', re.sub(r'^Authors?\s*:\s*', '', text, flags=re.I)) if s.strip()]
        parts.append(text)
    return dict(title=title, abstract=' '.join(parts), date_raw=date_raw, authors=authors,
                categories=categories, pdf_url=primary_files[0] if primary_files else None,
                pdf_urls=pdfs, attachment_pdf_urls=attachments, collection_pdf_urls=collection_files,
                reference_pdf_urls=[u for u in pdfs if u not in attachments],
                primary_pdf_binding=('explicit_download_list' if downloads else
                                     'explicit_publication_button' if buttons else
                                     'collection_without_primary_PDF' if collection_files else 'no_explicit_attachment'),
                report_links=report_links)


class AdrukOrgNewsPublicationsCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = 'Custom: adruk-org-news-publications'
    base_url = _BASE_URL
    DELIVERY_CURSOR_VERSION = 1

    def _html(self, url):
        self._check_cancelled()
        raw = _curl_get(url, check_cancelled=self._check_cancelled)
        self._check_cancelled()
        if not raw:
            raise ConnectionError('source HTML unavailable')
        return raw

    def _annual_sources(self):
        soup = _make_soup(self._html(_ANNUAL_INDEX))
        main = soup.find('main')
        if not main or 'annual report' not in _text(soup.find('h1')).lower():
            raise ValueError('missing annual index')
        roots = {}
        for a in main.find_all('a', href=True):
            url = urljoin(_ANNUAL_INDEX, a['href'])
            parsed = urlsplit(url)
            if parsed.netloc != 'reports.adruk.org':
                continue
            if (parsed.scheme != 'https' or parsed.query or parsed.fragment
                    or not re.fullmatch(r'/annual-report-(20\d{2})-(20\d{2})/', parsed.path)):
                raise ValueError('unreviewed annual report index target')
            roots[url] = _text(a)
        if not roots:
            raise ValueError('annual index contains no report references')
        reports = {}
        for url, label in roots.items():
            self._check_cancelled()
            year = re.search(r'20\d{2}', urlsplit(url).path).group()
            soup = _make_soup(self._html(url))
            main = soup.find('main')
            if not main or year not in _text(soup.find('h1')) or 'annual report' not in _text(soup.find('h1')).lower():
                raise ValueError('annual report identity mismatch')
            pdfs = set()
            for a in main.find_all('a', href=True):
                target = urldefrag(urljoin(url, a['href']))[0]
                path = unquote(urlsplit(target).path)
                if (path.lower().endswith('.pdf') and year in path
                        and 'annual' in path.lower() and 'report' in path.lower()):
                    self._validate_pdf_url(target)
                    pdfs.add(target)
            if len(pdfs) != 1:
                raise ValueError('annual report needs a unique own-report PDF')
            reports[url] = dict(label=label, year=year, pdf_url=pdfs.pop())
        self.delivery_annual_reports = reports
        return reports

    @staticmethod
    def _validate_pdf_url(url):
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.netloc not in ('www.adruk.org', 'reports.adruk.org')
                or not parsed.path.startswith('/fileadmin/') or not parsed.path.lower().endswith('.pdf')
                or parsed.query or parsed.fragment):
            raise ValueError('PDF outside reviewed publisher file scope')

    def _pdf_bytes(self, url):
        started = time.monotonic()
        for _ in range(4):
            self._check_cancelled()
            self._validate_pdf_url(url)
            with self._session.get(url, stream=True, timeout=(10, 30), allow_redirects=False) as response:
                if response.status_code in (401, 403, 429, 503):
                    raise PermissionError(f'HTTP {response.status_code}')
                if response.status_code in (301, 302, 303, 307, 308):
                    if not response.headers.get('Location'):
                        raise ValueError('PDF redirect without destination')
                    url = urljoin(url, response.headers['Location'])
                    continue
                if response.status_code != 200:
                    raise ConnectionError('PDF response unavailable')
                content = bytearray()
                if int(response.headers.get('Content-Length', '0')) > _PDF_MAX_BYTES:
                    raise ValueError('PDF size budget exceeded')
                for chunk in response.iter_content(65536):
                    self._check_cancelled()
                    content.extend(chunk)
                    if len(content) > _PDF_MAX_BYTES or time.monotonic() - started > 180:
                        raise ValueError('PDF download budget exceeded')
                if (response.headers.get('Content-Length') and not response.headers.get('Content-Encoding')
                        and int(response.headers['Content-Length']) != len(content)):
                    raise ValueError('incomplete PDF response')
                if not content.startswith(b'%PDF-'):
                    raise ValueError('publisher response is not PDF')
                return bytes(content)
        raise ValueError('PDF redirect budget exceeded')

    def _same_pdf(self, left, right):
        if left == right:
            return True
        # A matching filename on different hosts alone is not identity evidence.
        return hashlib.sha256(self._pdf_bytes(left)).digest() == hashlib.sha256(self._pdf_bytes(right)).digest()

    def _pdf_body(self, url):
        from delivery.files.worker import run_stage
        content = self._pdf_bytes(url)
        with tempfile.TemporaryDirectory(prefix='adruk-pdf-body-') as directory:
            root = Path(directory)
            (root / 'source.pdf').write_bytes(content)
            result = run_stage('text', root, timeout=60)
            self._check_cancelled()
            if not result.get('success'):
                raise ValueError('PDF body unavailable: ' + str(result.get('error')))
            with (root / 'output.txt').open(encoding='utf-8') as handle:
                abstract = re.sub(r'\s+', ' ', handle.read(12000)).strip()[:3000]
            with (root / 'output.txt').open('rb') as handle:
                text_hash = hashlib.file_digest(handle, 'sha256').hexdigest()
        return abstract, dict(abstract_source='publisher_pdf_text_excerpt', abstract_pdf_url=url,
                              abstract_pdf_sha256=hashlib.sha256(content).hexdigest(),
                              abstract_pdf_text_sha256=text_hash, abstract_pdf_text_characters=result.get('chars'))

    @staticmethod
    def _contract():
        return _hash([_SCOPES, _ANNUAL_INDEX])

    def _validate_cursor(self, cursor):
        if (not isinstance(cursor, dict) or cursor.get('version') != self.DELIVERY_CURSOR_VERSION
                or cursor.get('contract') != self._contract() or cursor.get('scope') not in _SCOPES):
            raise ValueError('changed cursor contract')
        for key in ('page', 'offset', 'first', 'last', 'total', 'previous_last'):
            if type(cursor.get(key)) is not int or cursor[key] < 0:
                raise ValueError('invalid cursor position')
        if (not 1 <= cursor['first'] <= cursor['last'] <= cursor['total']
                or cursor['offset'] > cursor['last'] - cursor['first'] + 1):
            raise ValueError('invalid cursor range')
        for key in ('page_hash', 'annual_hash'):
            if not re.fullmatch('[0-9a-f]{64}', cursor.get(key, '')):
                raise ValueError('invalid cursor anchor')
        if cursor['page'] == 0:
            if cursor['first'] != 1 or cursor['previous_last'] != 0 or cursor.get('previous_hash') != '':
                raise ValueError('invalid first-page anchor')
        elif (cursor['previous_last'] + 1 != cursor['first']
              or not re.fullmatch('[0-9a-f]{64}', cursor.get('previous_hash', ''))):
            raise ValueError('invalid previous-page anchor')
        matched = json.loads(cursor.get('annual_matched', ''))
        if not isinstance(matched, list) or any(not isinstance(x, str) for x in matched) or len(matched) != len(set(matched)):
            raise ValueError('invalid annual representation checkpoint')
        if cursor['scope'] == 'impact' and matched:
            raise ValueError('annual matches before publications scope')
        return set(matched)

    def _checkpoint(self, scope, page, offset, data, previous, matched, annual_hash, items_done=0):
        if not self._cursor_enabled:
            return
        self._advance_cursor(dict(version=self.DELIVERY_CURSOR_VERSION, contract=self._contract(), scope=scope,
                                  page=page, offset=offset, first=data['first'], last=data['last'], total=data['total'],
                                  page_hash=data['page_hash'], previous_last=previous[0], previous_hash=previous[1],
                                  annual_hash=annual_hash, annual_matched=json.dumps(sorted(matched))), items_done=items_done)

    def _assert_stored_identity(self, item):
        postgres = os.environ.get('LIBERTREE_DB_BACKEND', 'sqlite').lower() == 'postgres'
        if postgres or self._conn_is_libertree():
            placeholder = '%s' if postgres else '?'
            # The general dedup key contains BOTH post_number and meta_url;
            # it cannot detect a changed URL for an existing source identifier.
            rows = self._conn.execute(
                f'SELECT post_number,meta_url FROM documents WHERE site_id={placeholder} '
                f'AND (post_number={placeholder} OR meta_url={placeholder})',
                (self.site_id, item['slug'], item['url']),
            ).fetchall()
            if len(rows) > 1:
                raise ValueError('ambiguous stored source identity requires review')
            for row in rows:
                post, url = (row['post_number'], row['meta_url']) if hasattr(row, 'keys') else row
                if post != item['slug'] or url != item['url']:
                    raise ValueError('stored source identity requires review')

    def crawl(self, limit=None):
        self._check_cancelled()
        if limit is not None and limit <= 0:
            return 0
        self._source_failure = None
        self._exhausted = False
        self._pending_cursor = None
        self._cursor_items_done = 0
        started = time.monotonic()
        self._cursor_enabled = self.delivery_mode == 'backfill'
        resume = self.delivery_cursor if self._cursor_enabled else None
        matched = set()
        if resume is not None:
            try:
                matched = self._validate_cursor(resume)
            except Exception:
                self._report_source_failure('configuration')
                return 0
        saved, seen = 0, {}
        self.delivery_body_provenance = []
        try:
            if time.monotonic() - started > _WALL_CLOCK_LIMIT:
                raise ValueError('wall budget before progress')
            annual = self._annual_sources()
            annual_hash = _hash(annual)
            if resume and (resume['annual_hash'] != annual_hash or not matched <= set(annual)):
                raise ValueError('annual source representations changed')
        except PermissionError as exc:
            self._report_source_failure('rate_limited' if '429' in str(exc) else 'authentication_failed')
            return 0
        except ConnectionError:
            self._report_source_failure('fetch_failed')
            return 0
        except Exception:
            self._report_source_failure('invalid_response')
            return 0
        for scope in _SCOPES:
            if resume and list(_SCOPES).index(scope) < list(_SCOPES).index(resume['scope']):
                continue
            current = resume if resume and scope == resume['scope'] else None
            page, offset = (current['page'], current['offset']) if current else (0, 0)
            total = current['total'] if current else None
            previous = (current['previous_last'], current['previous_hash']) if current else (0, '')
            while True:
                self._check_cancelled()
                if limit is not None and saved >= limit:
                    return saved
                if time.monotonic() - started > _WALL_CLOCK_LIMIT:
                    if not self._cursor_enabled or self.delivery_cursor_items_done == 0:
                        self._report_source_failure('invalid_response')
                    return saved
                try:
                    if current and page > 0:
                        anchor = _listing(self._html(_list_url(scope, page - 1)), scope, page - 1)
                        if (anchor['last'], anchor['page_hash']) != previous or anchor['total'] != total:
                            raise ValueError('previous source page changed')
                    data = _listing(self._html(_list_url(scope, page)), scope, page)
                    if total is not None and data['total'] != total:
                        raise ValueError('source total changed')
                    total = data['total']
                    if total == 0:
                        if current:
                            raise ValueError('checkpointed page became empty')
                        break
                    if data['first'] != previous[0] + 1:
                        raise ValueError('source positions repeated or skipped')
                    if current and any(data[k] != current[k] for k in ('first', 'last', 'total', 'page_hash')):
                        raise ValueError('current source page changed')
                    if offset > len(data['items']):
                        raise ValueError('source page shorter than checkpoint')
                except PermissionError as exc:
                    self._report_source_failure('rate_limited' if '429' in str(exc) else 'authentication_failed')
                    return saved
                except ConnectionError:
                    self._report_source_failure('fetch_failed')
                    return saved
                except Exception:
                    self._report_source_failure('invalid_response')
                    return saved
                self._checkpoint(scope, page, offset, data, previous, matched, annual_hash)
                for index, item in enumerate(data['items']):
                    if index < offset:
                        continue
                    self._check_cancelled()
                    if limit is not None and saved >= limit:
                        return saved
                    if time.monotonic() - started > _WALL_CLOCK_LIMIT:
                        if not self._cursor_enabled or self.delivery_cursor_items_done == 0:
                            self._report_source_failure('invalid_response')
                        return saved
                    try:
                        if item['slug'] in seen and seen[item['slug']] != item['url']:
                            raise ValueError('source slug collision')
                        self._assert_stored_identity(item)
                        time.sleep(self._delay)
                        self._check_cancelled()
                        if item['direct_pdf']:
                            detail = dict(title=item['list_title'], abstract=item['list_abstract'], date_raw=item['listed_date_raw'],
                                          authors=[], categories=[], pdf_url=item['url'], pdf_urls=[item['url']],
                                          attachment_pdf_urls=[item['url']], reference_pdf_urls=[],
                                          primary_pdf_binding='direct_listing_PDF', report_links=[])
                        else:
                            detail = _parse_detail(self._html(item['url']))
                            if detail['title'] != item['list_title']:
                                raise ValueError('detail/list identity mismatch')
                        pdf_url = detail['pdf_url']
                        annual_match = None
                        if 'annual report' in detail['title'].lower():
                            year_match = re.search(r'20\d{2}', detail['title'])
                            candidates = [(url, r) for url, r in annual.items() if year_match and r['year'] == year_match.group()]
                            if len(candidates) > 1:
                                raise ValueError('ambiguous annual report identity')
                            if candidates:
                                url, report = candidates[0]
                                # The annual root identifies its own report;
                                # unrelated strategies/citations never win by order.
                                if url in detail['report_links']:
                                    pdf_url = report['pdf_url']
                                else:
                                    matching_pdfs = [u for u in detail.get('pdf_urls', [])
                                                     if self._same_pdf(u, report['pdf_url'])]
                                    if not matching_pdfs:
                                        raise ValueError('annual representation mismatch')
                                    pdf_url = matching_pdfs[0]
                                annual_match = url
                        abstract = detail['abstract'] or item['list_abstract']
                        provenance = {}
                        if len(abstract) < 50 and pdf_url:
                            abstract, provenance = self._pdf_body(pdf_url)
                        if len(abstract) < 50:
                            raise ValueError('source body gap')
                        paper = dict(site_id=self.site_id, external_id=item['slug'], title=detail['title'], abstract=abstract,
                                     published_date=detail['date_raw'] or item['listed_date_raw'] or None,
                                     posted_date=item['listed_date_raw'] or None, url=item['url'], pdf_url=pdf_url,
                                     original_filename=unquote(urlsplit(pdf_url).path.rsplit('/', 1)[-1]) if pdf_url else None,
                                     authors=detail['authors'], publisher=_PUBLISHER, keywords=', '.join(detail['categories']),
                                     metadata=json.dumps(dict(source_scope=scope, source_listed_date_raw=item['listed_date_raw'],
                                                              annual_report_url=annual_match,
                                                              source_pdf_candidates=detail.get('pdf_urls', []),
                                                              source_attachment_pdf_urls=detail.get('attachment_pdf_urls', []),
                                                              source_collection_pdf_urls=detail.get('collection_pdf_urls', []),
                                                              source_reference_pdf_urls=detail.get('reference_pdf_urls', []),
                                                              primary_pdf_binding='annual_report_identity' if annual_match else detail.get('primary_pdf_binding', 'direct_listing_PDF'),
                                                              **provenance), ensure_ascii=False))
                        self._save_paper(paper)
                        if self._last_save_created:
                            saved += 1
                        if provenance:
                            self.delivery_body_provenance.append(dict(post_number=item['slug'], **provenance))
                        if annual_match:
                            matched.add(annual_match)
                        seen[item['slug']] = item['url']
                        self._checkpoint(scope, page, index + 1, data, previous, matched, annual_hash, items_done=1)
                        self._check_cancelled()
                    except PermissionError as exc:
                        self._report_source_failure('rate_limited' if '429' in str(exc) else 'authentication_failed')
                        return saved
                    except Exception:
                        self._report_source_failure('item_failed')
                        if self._cursor_enabled:
                            return saved
                if data['last'] == total:
                    break
                previous = (data['last'], data['page_hash'])
                page, offset, current = page + 1, 0, None
        self._check_cancelled()
        if matched != set(annual):
            self._report_source_failure('item_failed')
        if not self.delivery_source_failure:
            self._mark_exhausted()
        return saved
