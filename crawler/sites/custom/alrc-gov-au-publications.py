# -*- coding: utf-8 -*-
"""ALRC final reports and the second, explicitly delivered PDF search scope.

Starting URL: https://www.alrc.gov.au/publications/final-report/

The ALRC site is WordPress, but its custom ``publication`` post type is not
publicly exposed through ``/wp-json/wp/v2``. The real listing is rendered as
HTML at ``/publications/final-report/`` and paginated with ``/page/N/`` URLs.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

# Absolute import is required because spec_from_file_location has no package
# context when these custom crawlers are loaded by tests/tools.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler
from crawler.alrc_scope import inspect_listing, list_url, same_url
from crawler.alrc_traversal import AlrcTraversal


SITE_ID = "alrc-gov-au-publications"
BASE_URL = "https://www.alrc.gov.au"
START_URL = f"{BASE_URL}/publications/final-report/"
PUBLISHER = "Australian Law Reform Commission"
MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
ABSTRACT_MIN_CHARS = 50
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _curl_get(url: str, *, timeout: int = 30) -> str:
    """Retain HTTP/access failures instead of parsing error pages as records."""
    cmd = [
        "curl",
        "-q",
        "--tls-max",
        "1.3",
        "--silent",
        "--show-error",
        "--proto", "=https",
        "--max-filesize", str(2 * 1024 ** 2),
        "--max-time",
        str(timeout),
        "-H",
        f"User-Agent: {USER_AGENT}",
        "-H",
        "Accept: text/html,application/xhtml+xml,application/json,*/*;q=0.8",
        "-H",
        "Accept-Language: en-AU,en;q=0.9",
        "--write-out", "\n%{http_code}",
        "--url", url,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5, check=False)
    body, _, code = result.stdout.rpartition(b"\n")
    status = int(code) if code.isdigit() else 0
    if status in (401, 403, 429, 503):
        raise PermissionError(f'ALRC HTTP {429 if status == 503 else status}')
    if result.returncode or status != 200 or not body.strip():
        raise ValueError('ALRC response incomplete or non-success')
    from delivery.health.access_challenge import access_challenge
    if access_challenge(body):
        raise PermissionError('ALRC access challenge')
    return body.decode('utf-8', errors='strict')


def _make_soup(raw: str):
    """Parse HTML with html5lib first, then lxml, then html.parser."""
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:
        print(f"[{SITE_ID}] BeautifulSoup unavailable: {exc}")
        return None

    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception as exc:
            print(f"[{SITE_ID}] BeautifulSoup {parser} failed: {exc}")
            continue
    return None


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = unescape(value).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_date(raw: str | None) -> str | None:
    """Normalize common ALRC/WordPress dates to YYYY-MM-DD when possible."""
    if not raw:
        return None
    text = _clean_text(raw)
    if not text:
        return None

    match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    if match:
        return match.group(1)

    text = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", text, flags=re.IGNORECASE)
    for fmt in (
        "%d.%m.%Y",
        "%d/%m/%Y",
        "%d %B %Y",
        "%d %b %Y",
        "%B %d, %Y",
        "%b %d, %Y",
    ):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _date_from_text(text: str) -> str | None:
    """Find a readable date inside prose such as 'on 6th March 2025'."""
    if not text:
        return None
    match = re.search(
        r"\b(\d{1,2}(?:st|nd|rd|th)?\s+"
        r"(?:Jan|January|Feb|February|Mar|March|Apr|April|May|Jun|June|"
        r"Jul|July|Aug|August|Sep|September|Oct|October|Nov|November|"
        r"Dec|December)\s+\d{4})\b",
        text,
        flags=re.IGNORECASE,
    )
    return _parse_date(match.group(1)) if match else None


def _list_page_url(page: int) -> str:
    if page <= 1:
        return START_URL
    return f"{START_URL}page/{page}/"


def _slug_from_url(url: str) -> str | None:
    path = urlparse(url).path.rstrip("/")
    if not path:
        return None
    return path.rsplit("/", 1)[-1] or None


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urlparse(url).path
    tail = unquote(path.rstrip("/").rsplit("/", 1)[-1])
    if tail and "." in tail and len(tail) <= 240:
        return tail
    return None


def _extract_wp_post_id(html: str, soup) -> str | None:
    """Extract native WordPress post ID from shortlink or body class."""
    patterns = (
        r"rel=['\"]shortlink['\"][^>]+href=['\"]https?://www\.alrc\.gov\.au/\?p=(\d+)",
        r"href=['\"]https?://www\.alrc\.gov\.au/\?p=(\d+)['\"][^>]+rel=['\"]shortlink['\"]",
        r"\bpostid-(\d+)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            return match.group(1)

    if soup and soup.body:
        classes = soup.body.get("class") or []
        for cls in classes:
            match = re.match(r"postid-(\d+)", str(cls))
            if match:
                return match.group(1)
    return None


def _json_ld_graph(soup) -> list[dict]:
    records: list[dict] = []
    if not soup:
        return records
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except Exception:
            continue
        if isinstance(data, dict):
            graph = data.get("@graph")
            if isinstance(graph, list):
                records.extend(x for x in graph if isinstance(x, dict))
            else:
                records.append(data)
        elif isinstance(data, list):
            records.extend(x for x in data if isinstance(x, dict))
    return records


def _meta_content(soup, *names: str) -> str:
    if not soup:
        return ""
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find(
            "meta", attrs={"name": name}
        )
        if tag and tag.get("content"):
            return _clean_text(tag.get("content"))
    return ""


def _parse_list_page(html: str) -> tuple[list[dict], bool]:
    """Return list-page stubs and whether a next-page link exists."""
    soup = _make_soup(html)
    if not soup:
        return [], False

    stubs: list[dict] = []
    for row in soup.select(".section-listing-item"):
        try:
            link = row.select_one("h2 a[href]") or row.select_one("a[href]")
            if not link:
                continue
            href = (link.get("href") or "").strip()
            if not href:
                continue
            url = urljoin(BASE_URL, href)
            title = _clean_text(link.get_text(" ", strip=True)) or _clean_text(
                link.get("title")
            )
            if not title:
                continue

            date_el = row.select_one(".section-listing-item-date")
            listed_raw = _clean_text(date_el.get_text(" ", strip=True) if date_el else "")

            paragraphs = [
                _clean_text(p.get_text(" ", strip=True))
                for p in row.find_all("p")
                if _clean_text(p.get_text(" ", strip=True))
            ]
            snippet = max(paragraphs, key=len) if paragraphs else ""

            stubs.append(
                {
                    "url": url,
                    "title": title,
                    "slug": _slug_from_url(url),
                    "listed_date_raw": listed_raw,
                    "listed_date": _parse_date(listed_raw),
                    "snippet": snippet,
                    "source": "html.section-listing-item",
                    "type_label": _clean_text(node.get_text(' ', strip=True)) if
                        (node := row.select_one('.section-listing-item-type')) else None,
                }
            )
        except Exception as exc:
            print(f"[{SITE_ID}] list item parse failed: {exc}")
            continue

    has_next = soup.select_one(".tw-pagination a.next[href]") is not None
    return stubs, has_next


def _best_pdf_link(soup) -> str | None:
    """Prefer the final report PDF, then the explicit Download PDF widget."""
    if not soup:
        return None

    pdf_links: list[tuple[int, str]] = []
    root = _content_root(soup)
    links = list(root.find_all('a', href=True)) if root else []
    links.extend(soup.select('#main-content .alrc-publication-action-pdf a[href]'))
    for index, link in enumerate(links):
        href = link.get("href") or ""
        if not urlparse(href).path.lower().endswith('.pdf'):
            continue
        text = _clean_text(" ".join([link.get_text(" ", strip=True), link.get("title") or ""]))
        score = 0
        low = text.lower()
        if "final report" in low:
            score += 100
        if "download pdf" in low:
            score += 50
        if "summary" in low or "appendix" in low or "appendices" in low:
            score -= 40
        pdf_links.append((score - index, urljoin(BASE_URL, href)))

    if not pdf_links:
        return None
    pdf_links.sort(reverse=True)
    return pdf_links[0][1]


def _content_root(soup):
    if not soup:
        return None
    return (
        soup.select_one("#main-content main.content")
        or soup.find("main")
        or soup.find("article")
    )


def _extract_abstract(soup) -> str:
    root = _content_root(soup)
    parts: list[str] = []
    if root:
        for noisy in root.find_all(["script", "style", "nav", "form", "aside", "footer"]):
            try:
                noisy.decompose()
            except Exception:
                pass
        body = root.select_one('.wysiwyg') or root
        blocks = ('p', 'li', 'td', 'th', 'blockquote', 'h2', 'h3', 'h4', 'h5', 'h6')
        for tag in body.find_all(blocks):
            if any(parent.name in blocks for parent in tag.parents if parent is not body):
                continue
            if 'post-date' in tag.get('class', []):
                continue
            text = _clean_text(tag.get_text(" ", strip=True))
            if not text:
                continue
            low = text.lower()
            if low.startswith("download ") or low in {"download pdf"}:
                continue
            if low.startswith("filed under:"):
                continue
            if "quick exit" in low or "stay informed" in low:
                continue
            parts.append(text)

    abstract = _clean_text(" ".join(parts))
    return abstract


def _parse_detail_page(html: str, url: str, stub: dict) -> dict:
    soup = _make_soup(html)
    if not soup:
        clean = _clean_text(re.sub(r"<[^>]+>", " ", html))
        return {
            "title": stub.get("title") or "",
            "abstract": clean or stub.get("snippet") or "",
            "published_date": stub.get("listed_date"),
            "authors": "",
            "wp_post_id": None,
            "pdf_url": None,
            "original_filename": None,
            "category": "Final Report",
            "keywords": "",
            "metadata": {"detail_parser": "regex-fallback"},
        }

    graph = _json_ld_graph(soup)
    article = next((x for x in graph if x.get("@type") == "Article"), {})
    webpage = next((x for x in graph if x.get("@type") == "WebPage"), {})

    h1 = soup.select_one("h1.post-title") or soup.find("h1")
    title = (
        _clean_text(h1.get_text(" ", strip=True) if h1 else "")
        or _clean_text(article.get("headline"))
        or _meta_content(soup, "og:title", "twitter:title")
        or stub.get("title")
        or ""
    )

    og_description = _meta_content(soup, "og:description", "description")
    abstract = _extract_abstract(soup)

    pdf_url = _best_pdf_link(soup)
    original_filename = _filename_from_url(pdf_url)
    wp_post_id = _extract_wp_post_id(html, soup)

    published_date = (
        _parse_date(article.get("datePublished"))
        or _parse_date(webpage.get("datePublished"))
        or stub.get("listed_date")
    )
    modified_date = _parse_date(article.get("dateModified")) or _parse_date(
        webpage.get("dateModified")
    )

    tabled_date = None
    if abstract and "tabled" in abstract.lower():
        tabled_date = _date_from_text(abstract)

    author = article.get("author")
    page_authors = ""
    if isinstance(author, dict):
        page_authors = _clean_text(author.get("name"))
    elif isinstance(author, list):
        page_authors = "; ".join(
            _clean_text(a.get("name")) for a in author if isinstance(a, dict) and a.get("name")
        )

    metadata = {
        "posted_date": stub.get("listed_date_raw"),
        "listed_date": stub.get("listed_date"),
        "originalFilename": original_filename,
        "journal_raw": None,
        "series": None,
        "volume": None,
        "issue": None,
        "wp_post_id": wp_post_id,
        "node_id": wp_post_id,
        "slug": stub.get("slug") or _slug_from_url(url),
        "source_list_url": START_URL,
        "list_source": stub.get("source"),
        "list_snippet": stub.get("snippet"),
        "date_published_raw": article.get("datePublished") or webpage.get("datePublished"),
        "date_modified_raw": article.get("dateModified") or webpage.get("dateModified"),
        "date_modified": modified_date,
        "tabled_date": tabled_date,
        "og_description": og_description,
        "page_authors": page_authors,
        "json_ld_article": article,
    }

    return {
        "title": title,
        "abstract": abstract,
        "published_date": published_date,
        "authors": "",
        "wp_post_id": wp_post_id,
        "pdf_url": pdf_url,
        "original_filename": original_filename,
        "category": "Final Report",
        "keywords": "",
        "metadata": metadata,
    }


class ALRCGovAuPublicationsCrawler(AlrcTraversal, BaseCrawler):
    """Traverse both delivered source lists with restartable source anchors."""

    site_id = "alrc-gov-au-publications"
    site_name = "Custom: alrc-gov-au-publications"
    base_url = "https://www.alrc.gov.au"

    def _alrc_wall_seconds(self):
        return MAX_WALL_SECONDS

    def _alrc_fetch(self, url):
        self._check_cancelled()
        time.sleep(self._delay)
        self._check_cancelled()
        raw = _curl_get(url)
        self._check_cancelled()
        if not raw or not re.search(r'</html\s*>', raw, re.I):
            raise ValueError('ALRC HTML document incomplete')
        return raw

    def _alrc_list(self, scope, page):
        raw = self._alrc_fetch(list_url(scope, page))
        items, _ = _parse_list_page(raw)
        for item in items:
            parsed = urlparse(item['url'])
            if parsed.scheme != 'https' or parsed.netloc != 'www.alrc.gov.au' or parsed.fragment:
                raise ValueError('ALRC listing document outside source host')
        return items, inspect_listing(_make_soup(raw), items, scope, page)

    def _alrc_paper(self, item, scope, source_list_url):
        item_url = item['url']
        raw = self._alrc_fetch(item_url)
        soup = _make_soup(raw)
        canonical = soup.select_one('link[rel="canonical"][href]') if soup else None
        if not canonical or not same_url(canonical['href'], item_url) or not soup.find('h1'):
            raise ValueError('ALRC detail URL identity missing or changed')
        detail = _parse_detail_page(raw, item_url, item)
        title = detail.get('title') or ''
        abstract = _clean_text(detail.get('abstract') or '')
        if not title or len(abstract) < ABSTRACT_MIN_CHARS:
            return None
        native_id = detail.get('wp_post_id') or item.get('slug') or item_url
        post_number = detail.get('wp_post_id') or item.get('slug')
        metadata = dict(detail.get('metadata') or {})
        metadata.update(posted_date=item.get('listed_date_raw'), post_number=post_number,
                        external_id=native_id, detail_url=item_url,
                        source_list_url=source_list_url, source_scope=scope,
                        source_type_label=item.get('type_label'))
        return dict(id=f'{self.site_id}:{native_id}', site_id=self.site_id,
                    external_id=str(native_id), post_number=post_number, title=title,
                    abstract=abstract,
                    published_date=metadata.get('date_published_raw') or item.get('listed_date_raw'),
                    listed_date=item.get('listed_date_raw'), posted_date=item.get('listed_date_raw'),
                    authors=detail.get('authors') or '', publisher=PUBLISHER,
                    department='', journal='', url=item_url, pdf_url=detail.get('pdf_url'),
                    keywords=detail.get('keywords') or '',
                    category=item.get('type_label') or ('Final Report' if scope == 'final-report' else ''),
                    doi='', original_filename=detail.get('original_filename'),
                    metadata=json.dumps(metadata, ensure_ascii=False, default=str))

    # ------------------------------------------------------------------
    # Pure enumeration contract (L3b-20261018). crawl() (AlrcTraversal) and
    # the existing methods are unchanged; these methods only parse already
    # fetched HTML. Scope: the first delivered list, 'final-report'
    # (/publications/final-report/, ?paged=N -> 301 /page/N/).
    # ------------------------------------------------------------------

    _ENUMERATION_SCOPE = 'final-report'

    @classmethod
    def _enumeration_scope(cls, url):
        """Delivered scope of a list URL (X2 multi_listing): the PDF search
        list (/?s=pdf&type=all, /page/N/?s=pdf&type=all) is 'delivered-search',
        anything else the final-report list (the X1 plan's only scope)."""
        from urllib.parse import parse_qs, urlsplit
        parts = urlsplit(url or START_URL)
        query = parse_qs(parts.query, keep_blank_values=True)
        if query.get('s') == ['pdf'] and re.fullmatch(r'/(?:page/\d+/?)?', parts.path or '/'):
            return 'delivered-search'
        return cls._ENUMERATION_SCOPE

    def _enumeration_items(self, html, url=None):
        """Rows of one list page of either delivered scope, parsed and validated
        as _alrc_list() does (same host check, same publisher-pager check)."""
        from urllib.parse import parse_qs, urlsplit
        from crawler.enumeration.http import PermanentFailure
        if not isinstance(html, str):
            raise PermanentFailure('listing_parse_error:expected_html', url)
        scope = self._enumeration_scope(url)
        parts = urlsplit(url or START_URL)
        match = re.search(r'/page/(\d+)/?$', parts.path)
        paged = (parse_qs(parts.query).get('paged') or [None])[0]
        page = int(match.group(1)) if match else int(paged) if paged and paged.isdigit() else 1
        page = max(page, 1)
        try:
            items, _ = _parse_list_page(html)
            if not items:
                # Past the last page the source answers 200 with an empty
                # section.section-listing (no rows, no pager); the plan's
                # listing_marker tells it apart from an error page.
                return []
            for item in items:
                parsed = urlparse(item['url'])
                if parsed.scheme != 'https' or parsed.netloc != 'www.alrc.gov.au' or parsed.fragment:
                    raise ValueError('ALRC listing document outside source host')
            inspect_listing(_make_soup(html), items, scope, page)
        except ValueError as exc:
            raise PermanentFailure('listing_parse_error:alrc_listing_invalid', url) from exc
        source_list_url = list_url(scope, page)
        rows = [dict(item, source_list_url=source_list_url) for item in items]
        if scope != self._ENUMERATION_SCOPE:
            rows = [dict(row, source_scope=scope) for row in rows]
        return rows

    def _enumeration_paper(self, html, item):
        """Paper from a fetched detail page — _alrc_paper() copied verbatim with
        the fetched HTML in place of _alrc_fetch() (identity unchanged:
        external_id = WordPress post id, else slug). The scope is the row's
        list (source_scope, set only for the delivered-search list)."""
        scope = item.get('source_scope') or self._ENUMERATION_SCOPE
        source_list_url = item['source_list_url']
        item_url = item['url']
        raw = html
        if not raw or not re.search(r'</html\s*>', raw, re.I):
            raise ValueError('ALRC HTML document incomplete')
        soup = _make_soup(raw)
        canonical = soup.select_one('link[rel="canonical"][href]') if soup else None
        if not canonical or not same_url(canonical['href'], item_url) or not soup.find('h1'):
            raise ValueError('ALRC detail URL identity missing or changed')
        detail = _parse_detail_page(raw, item_url, item)
        title = detail.get('title') or ''
        abstract = _clean_text(detail.get('abstract') or '')
        if not title or len(abstract) < ABSTRACT_MIN_CHARS:
            return None
        native_id = detail.get('wp_post_id') or item.get('slug') or item_url
        post_number = detail.get('wp_post_id') or item.get('slug')
        metadata = dict(detail.get('metadata') or {})
        metadata.update(posted_date=item.get('listed_date_raw'), post_number=post_number,
                        external_id=native_id, detail_url=item_url,
                        source_list_url=source_list_url, source_scope=scope,
                        source_type_label=item.get('type_label'))
        return dict(id=f'{self.site_id}:{native_id}', site_id=self.site_id,
                    external_id=str(native_id), post_number=post_number, title=title,
                    abstract=abstract,
                    published_date=metadata.get('date_published_raw') or item.get('listed_date_raw'),
                    listed_date=item.get('listed_date_raw'), posted_date=item.get('listed_date_raw'),
                    authors=detail.get('authors') or '', publisher=PUBLISHER,
                    department='', journal='', url=item_url, pdf_url=detail.get('pdf_url'),
                    keywords=detail.get('keywords') or '',
                    category=item.get('type_label') or ('Final Report' if scope == 'final-report' else ''),
                    doi='', original_filename=detail.get('original_filename'),
                    metadata=json.dumps(metadata, ensure_ascii=False, default=str))
