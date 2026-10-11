# -*- coding: utf-8 -*-
"""Central Bank of Ireland — Annual Reports publication crawler.

Starting URL: https://www.centralbank.ie/publication/corporate-reports/annual-reports

All records are embedded in the listing page HTML as a JavaScript ``appData``
array. Both delivered annual-report and press-release scopes are traversed,
including the linked archive of press-release PDF compilations. Source-only
abstracts and stable per-page checkpoints are retained.
"""

import json
import hashlib
from datetime import datetime
import os
import re
import subprocess
import sys
import time
import urllib.parse

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_BASE = "https://www.centralbank.ie"
_LIST_URL = f"{_BASE}/publication/corporate-reports/annual-reports"
_SITE_ID = "centralbank-ie-publication"
_PUBLISHER = "Central Bank of Ireland"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _curl_get(url: str, timeout: int = 30, retries: int = 3) -> str | None:
    """GET via curl with TLS-max 1.3 and exponential-backoff retries (1s, 3s, 9s)."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", f"User-Agent: {_UA}",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: en-IE,en;q=0.9",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            raw = result.stdout.decode("utf-8", errors="replace")
            if raw.strip():
                return raw
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[{_SITE_ID}] empty response, retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[{_SITE_ID}] curl error: {exc}, retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
            else:
                print(f"[{_SITE_ID}] curl failed after {retries} attempts: {exc}")
    return None


# ---------------------------------------------------------------------------
# HTML / data helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("no HTML parser available")


def _parse_date(raw: str) -> str:
    """Convert 'DD/MM/YYYY' → 'YYYY-MM-DD'. Returns raw string on failure."""
    if not raw:
        return ""
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", raw.strip())
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return raw.strip()


def _extract_year(doc_name: str) -> str:
    """Return the first 4-digit year (2000-2099 or 1900-1999) found in *doc_name*."""
    m = re.search(r"\b(20\d{2})\b", doc_name)
    if not m:
        m = re.search(r"\b(19\d{2})\b", doc_name)
    return m.group(1) if m else ""


def _parse_app_data(html: str) -> list[dict]:
    """Extract the embedded ``appData`` JS array from the listing page HTML.

    The JS objects look like::

        { "type": "pdf", "date": "29/05/2025",
          "documentName": decodeTitle("Annual Report 2024..."),
          "url": decodeTitle("/docs/.../file.pdf?sfvrsn=xxx"),
          "translatedVersion": "", "size": "8340 KB" }

    After unwrapping ``decodeTitle("...")`` each object is valid JSON.
    """
    m = re.search(r"var appData\s*=\s*(\[.*?\]);", html, re.DOTALL)
    if not m:
        return []

    raw = m.group(1)
    # Unwrap decodeTitle("...") → the raw string content
    raw = re.sub(
        r'decodeTitle\("([^"]*)"\)',
        lambda x: '"' + x.group(1) + '"',
        raw,
    )

    records: list[dict] = []
    for obj_m in re.finditer(r'\{[^{}]+\}', raw, re.DOTALL):
        obj_str = obj_m.group(0)
        try:
            rec = json.loads(obj_str)
            records.append(rec)
        except json.JSONDecodeError:
            # Fallback: extract key-value pairs manually
            rec: dict = {}
            for kv in re.finditer(r'"(\w+)"\s*:\s*"([^"]*)"', obj_str):
                rec[kv.group(1)] = kv.group(2)
            if rec:
                records.append(rec)
    return records


def _fetch_detail_text(year: str) -> str:
    """Fetch the CBI detail page for *year* and return main article text.

    Only attempted for recent years (≥ 2020); older years lack individual pages.
    Returns empty string on any failure or when the page is a 404.
    """
    try:
        if int(year) < 2020:
            return ""
    except ValueError:
        return ""

    url = (
        f"{_BASE}/publication/corporate-reports/"
        f"central-bank-annual-report-and-annual-performance-statement-{year}"
    )
    raw = _curl_get(url)
    if not raw:
        return ""
    if "page not found" in raw.lower() or "page-not-found" in raw[:500].lower():
        return ""

    try:
        soup = _make_soup(raw)
    except Exception:
        return ""

    try:
        for tag in soup.find_all(
            ["nav", "header", "footer", "script", "style", "noscript", "aside"]
        ):
            tag.decompose()

        article = (
            soup.find("article")
            or soup.find(id=re.compile(r"MainContent", re.I))
            or soup.find("main")
        )
        if not article:
            return ""

        parts: list[str] = []
        for el in article.find_all(["p", "h2", "h3"]):
            t = el.get_text(" ", strip=True)
            if t and len(t) > 20:
                parts.append(t)

        text = re.sub(r"\s+", " ", " ".join(parts)).strip()
        return text if text else ""

    except Exception:
        return ""


_PRESS_URL = _BASE + '/news-media/press-releases'
_ARCHIVE_URL = _PRESS_URL + '/press-release-archive'


def _source_date(raw, fmt='%d/%m/%Y'):
    try:
        return datetime.strptime(raw.strip(), fmt).date().isoformat()
    except (ValueError, TypeError, AttributeError):
        return None


def _records(html):
    match = re.search(r'var appData\s*=\s*(\[.*?\]);', html, re.S)
    if not match:
        raise ValueError('source appData array missing')
    raw = re.sub(r'decodeTitle\(("(?:[^"\\]|\\.)*")\)', r'\1', match[1])
    records = json.loads(re.sub(r',\s*]', ']', raw))
    if not isinstance(records, list) or not records:
        raise ValueError('source appData array empty or invalid')
    urls = []
    for rec in records:
        if not rec.get('documentName') or not rec.get('url'):
            raise ValueError('source document identity missing')
        urls.append(urllib.parse.urljoin(_BASE, rec['url']))
    if len(urls) != len(set(urls)):
        raise ValueError('source appData repeats a document URL')
    return records


class CentralBankIEPublicationCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = 'Custom: centralbank-ie-publication'
    base_url = _BASE
    DELIVERY_STOP_ON_KNOWN = False

    def _fetch(self, url):
        self._check_cancelled()
        time.sleep(self._delay)
        raw = _curl_get(url)
        self._check_cancelled()
        if not raw:
            raise ValueError('source fetch failed: ' + url)
        return raw

    def _parse_press_page(self, raw, page):
        soup = _make_soup(raw)
        heading = soup.select_one('main h1')
        if not heading or heading.get_text(' ', strip=True) != 'Press Releases':
            raise ValueError('press catalogue heading missing')
        items = []
        for h in soup.select('h2.spotlight-title'):
            a = h.select_one('a[href]')
            if not a:
                raise ValueError('press item URL missing')
            url = urllib.parse.urljoin(_BASE, a['href'])
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme != 'https' or parsed.netloc != 'www.centralbank.ie' or not parsed.path.startswith('/news/article/'):
                raise ValueError('press item URL outside original scope')
            labels = [x.get_text(' ', strip=True) for x in h.parent.select('.spotlight-label')]
            items.append(dict(url=url, title=a.get_text(' ', strip=True), date_raw=labels[0] if labels else ''))
        if len({x['url'] for x in items}) != len(items):
            raise ValueError('press catalogue repeats a document')
        next_links = [a for a in soup.select('.pagination a[href]') if a.get_text(' ', strip=True) == 'Next page']
        if len(next_links) > 1:
            raise ValueError('ambiguous next page')
        next_page = None
        if next_links and next_links[0]['href'] != '#':
            url = urllib.parse.urljoin(_BASE, next_links[0]['href'])
            if url != _PRESS_URL + '/' + str(page + 1):
                raise ValueError('source next page skips, repeats or leaves press scope')
            next_page = page + 1
        if not items and next_page:
            raise ValueError('empty press page advertises further results')
        if items and not next_links:
            raise ValueError('press pagination end not established')
        return items, next_page

    def _press_paper(self, item):
        soup = _make_soup(self._fetch(item['url']))
        article = soup.select_one('article')
        if not article or not article.select_one('h1'):
            raise ValueError('press article body missing')
        title = article.select_one('h1').get_text(' ', strip=True)
        tags = article.select_one('.news-tags')
        date_raw = tags.select_one('span').get_text(' ', strip=True) if tags and tags.select_one('span') else ''
        publication = _source_date(date_raw, '%d %B %Y')
        pdfs = []
        for a in article.select('a[href]'):
            url = urllib.parse.urljoin(item['url'], a['href'])
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme in ('http', 'https') and parsed.path.lower().endswith('.pdf') and url not in pdfs:
                pdfs.append(url)
        article.select_one('h1').decompose()
        if tags:
            tags.decompose()
        for node in article.select('script,style,nav'):
            node.decompose()
        abstract = article.get_text(' ', strip=True)
        if not title or not abstract:
            raise ValueError('press article has no source title or body')
        identity = 'press:' + urllib.parse.urlsplit(item['url']).path
        return dict(site_id=self.site_id, external_id=identity, post_number=identity,
            title=title, abstract=abstract, url=item['url'], pdf_url=pdfs[0] if pdfs else None,
            published_date=publication, listed_date=_source_date(item['date_raw'], '%d %B %Y'),
            publisher=_PUBLISHER, category='Press Releases',
            original_filename=urllib.parse.unquote(urllib.parse.urlsplit(pdfs[0]).path.rsplit('/', 1)[-1]) if pdfs else None,
            metadata=json.dumps(dict(publication_date_raw=date_raw, publication_date_role='article_date',
                listing_date_raw=item['date_raw'], source_pdf_urls=pdfs, abstract_source='original article body'), ensure_ascii=False))

    def _pdf_paper(self, rec, scope):
        name = rec['documentName'].strip()
        url = urllib.parse.urljoin(_BASE, rec['url'])
        filename = urllib.parse.urlsplit(url).path.rsplit('/', 1)[-1]
        if not filename.lower().endswith('.pdf'):
            raise ValueError('source document is not a PDF URL')
        external_id = re.sub(r'\.pdf$', '', filename, flags=re.I)
        raw_date = rec.get('date', '')
        year = _extract_year(name)
        annual = scope == 'annual'
        # Retain all existing annual external IDs, post numbers and metadata URLs.
        detail_url = (_BASE + '/publication/corporate-reports/central-bank-annual-report-and-annual-performance-statement-' + year
                      if year and int(year) >= 2020 else _LIST_URL) if annual else url
        self._check_cancelled()
        if annual and year:
            time.sleep(self._delay)
        abstract = _fetch_detail_text(year) if annual and year else ''
        self._check_cancelled()
        if not annual:
            external_id = 'press-archive:' + external_id
        date_value = _source_date(raw_date)
        return dict(site_id=self.site_id, external_id=external_id, post_number=(year or external_id) if annual else external_id,
            title=name, abstract=abstract, authors=None, publisher=_PUBLISHER, department=_PUBLISHER,
            journal=None, category='Annual Reports' if annual else 'Press Release Archive',
            keywords='annual report,central bank,ireland,corporate report' if annual else 'press release archive,central bank,ireland',
            published_date=date_value if annual else None, listed_date=date_value, url=detail_url,
            pdf_url=url, doi=None, original_filename=filename,
            metadata=json.dumps(dict(posted_date=raw_date if annual else None, originalFilename=filename,
                size=rec.get('size',''), translatedVersion=rec.get('translatedVersion',''),docType=rec.get('type','pdf'),year=year,
                listing_date_raw=raw_date,listing_date_role='annual_report_listing_date' if annual else 'archive_index_date_not_article_publication',
                abstract_source='original annual report detail' if abstract else 'source PDF full text pending'),ensure_ascii=False))

    def crawl(self, limit=None):
        self._check_cancelled()
        if limit is not None and limit <= 0:
            return 0
        self._exhausted = False
        self._source_failure = None
        cursor = dict(self.delivery_cursor or {})
        scope = cursor.get('scope', 'annual')
        page = cursor.get('page', 1)
        index = cursor.get('next_index', 0)
        if (scope not in ('annual','press','archive','done') or type(page) is not int or page < 1
                or type(index) is not int or index < 0 or cursor.get('version', 1) != 1):
            raise ValueError('invalid source cursor')
        expected = cursor.get('list_digest', '')
        saved = 0
        start = time.monotonic()
        budget = int(os.environ.get('LIBERTREE_MAX_WALL_S', str(25*60)))
        while scope != 'done':
            self._check_cancelled()
            if limit is not None and saved >= limit:
                return saved
            if time.monotonic() - start >= budget:
                return saved
            url = _LIST_URL if scope == 'annual' else _ARCHIVE_URL if scope == 'archive' else _PRESS_URL + ('/'+str(page) if page > 1 else '')
            try:
                raw = self._fetch(url)
                if scope == 'press':
                    items, next_page = self._parse_press_page(raw, page)
                    identities = [x['url'] for x in items]
                else:
                    items = _records(raw)
                    identities = [urllib.parse.urljoin(_BASE, x['url']) for x in items]
                    next_page = None
                digest = hashlib.sha256(json.dumps(identities,ensure_ascii=False).encode()).hexdigest()
                if (expected and expected != digest) or index > len(items):
                    raise ValueError('source list changed since checkpoint; restart overlap review required')
                for offset in range(index, len(items)):
                    self._check_cancelled()
                    if limit is not None and saved >= limit:
                        return saved
                    if time.monotonic() - start >= budget:
                        return saved
                    paper = self._press_paper(items[offset]) if scope == 'press' else self._pdf_paper(items[offset], scope)
                    self._save_paper(paper)
                    if self._last_save_created:
                        saved += 1
                    self._advance_cursor(dict(version=1,scope=scope,page=page,next_index=offset+1,list_digest=digest),items_done=1)
            except Exception:
                self._report_source_failure('invalid_response')
                raise
            if scope == 'annual':
                scope, page = 'press', 1
            elif scope == 'press' and next_page:
                page = next_page
            elif scope == 'press':
                scope, page = 'archive', 1
            else:
                scope, page = 'done', 1
            index, expected = 0, ''
            self._advance_cursor(dict(version=1,scope=scope,page=page,next_index=0,list_digest=''))
        self._check_cancelled()
        self._mark_exhausted()
        return saved
