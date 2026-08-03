# -*- coding: utf-8 -*-
"""ANSTO (Australian Nuclear Science and Technology Organisation) search crawler.

Starting URL: https://www.ansto.gov.au/search?query=pdf
Pagination: Drupal 10 Views AJAX at /views/ajax?...&query=pdf&page=N (0-indexed, 20/page, ~2048 results)
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_SITE_ID = "ansto-gov-au-search"
_BASE = "https://www.ansto.gov.au"
_AJAX_URL = f"{_BASE}/views/ajax"
_QUERY = "pdf"
_MIN_ABSTRACT = 100   # skip items whose abstract is shorter than this
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MAX_WALL = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))   # 25-minute wall-clock budget


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _curl_get(
    url: str,
    *,
    timeout: int = 30,
    retries: int = 3,
    extra_headers: list[str] | None = None,
) -> str | None:
    """Fetch *url* via curl (TLS-max 1.3). Returns decoded text or None."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", ("User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "-H", "Accept: text/html,application/xhtml+xml,application/json,*/*;q=0.8",
        "-H", "Accept-Language: en-AU,en;q=0.9",
    ]
    for h in (extra_headers or []):
        cmd += ["-H", h]
    cmd.append(url)
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 15)
            raw = result.stdout.decode("utf-8", errors="replace")
            if raw.strip():
                return raw
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)   # 1 s, 3 s, 9 s
                print(f"[{_SITE_ID}] empty response for {url}, "
                      f"retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[{_SITE_ID}] curl error: {exc}, "
                      f"retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
            else:
                print(f"[{_SITE_ID}] curl failed after {retries} attempts for {url}: {exc}")
    return None


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _strip_tags(html: str) -> str:
    """Strip HTML tags and normalise whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Data extraction helpers
# ---------------------------------------------------------------------------

def _extract_iso_date(raw: str) -> str:
    """Return YYYY-MM-DD from an ISO-8601 datetime string, or ''."""
    if not raw:
        return ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw.strip())
    return m.group(1) if m else ""


def _original_filename(pdf_url: str) -> str:
    """Extract and URL-decode the filename from a PDF URL path."""
    if not pdf_url:
        return ""
    try:
        path = urllib.parse.urlparse(pdf_url).path
        name = urllib.parse.unquote(path.split("/")[-1])
        return name if name.lower().endswith(".pdf") else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Search list page
# ---------------------------------------------------------------------------

def _fetch_search_page(page_num: int) -> list[dict]:
    """Fetch one page of Drupal Views AJAX search results.

    Returns list of {'title': str, 'url': str} dicts (may be empty).
    """
    params = (
        "view_name=search&view_display_id=page"
        "&view_args=&view_path=/search&view_base_path=search"
        "&view_dom_id=ansto_crawl_dom&pager_element=0"
        f"&query={_QUERY}&page={page_num}"
    )
    url = f"{_AJAX_URL}?{params}"
    raw = _curl_get(
        url,
        extra_headers=[
            "X-Requested-With: XMLHttpRequest",
            "Referer: https://www.ansto.gov.au/search?query=pdf",
        ],
    )
    if not raw:
        return []

    # Drupal sometimes wraps AJAX JSON in <textarea>…</textarea>
    raw = raw.strip()
    if raw.startswith("<textarea"):
        m = re.search(r"<textarea[^>]*>(.*?)</textarea>", raw, re.DOTALL)
        raw = m.group(1) if m else raw

    try:
        resp = json.loads(raw)
    except Exception as exc:
        print(f"[{_SITE_ID}] JSON parse error on search page {page_num}: {exc}")
        return []

    # The AJAX response is a list of command dicts.  The 'insert' command
    # whose method is 'replaceWith' contains the rendered result HTML.
    html = ""
    for cmd in resp:
        if cmd.get("command") == "insert" and cmd.get("method") == "replaceWith":
            html = cmd.get("data", "")
            break

    if not html:
        return []

    items: list[dict] = []
    # Each result teaser renders as: <h2><a href="/path">Title</a></h2>
    for m in re.finditer(r'<h2><a href="(/[^"]+)"[^>]*>([^<]+)</a></h2>', html):
        path = m.group(1)
        title = m.group(2).strip()
        items.append({"title": title, "url": _BASE + path})
    return items


# ---------------------------------------------------------------------------
# Detail page
# ---------------------------------------------------------------------------

def _fetch_detail(url: str) -> dict:
    """Fetch a content detail page and extract metadata.

    Returns a dict with keys: abstract, pdf_url, published_date,
    node_id, original_filename.  Empty strings for missing values.
    Returns {} on total fetch failure.
    """
    raw = _curl_get(url)
    if not raw:
        return {}

    # --- Drupal node ID (from embedded settings JSON) ---
    node_id = ""
    m = re.search(r'"currentPath"\s*:\s*"node\\/(\d+)"', raw)
    if not m:
        m = re.search(r'"currentPath"\s*:\s*"node/(\d+)"', raw)
    if m:
        node_id = m.group(1)

    # --- Abstract + published date from JSON-LD ---
    abstract = ""
    published_date = ""
    jm = re.search(
        r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>',
        raw, re.DOTALL,
    )
    if jm:
        try:
            data = json.loads(jm.group(1))
            graph = data.get("@graph", [data])
            if not isinstance(graph, list):
                graph = [graph]
            for item in graph:
                if not abstract and item.get("description"):
                    abstract = item["description"].strip()
                if not published_date and item.get("datePublished"):
                    published_date = _extract_iso_date(item["datePublished"])
        except Exception:
            pass

    # --- Fallback: body text via BS4 when JSON-LD description is short ---
    if len(abstract) < _MIN_ABSTRACT:
        try:
            soup = _make_soup(raw)
            if soup:
                main = (
                    soup.find("main")
                    or soup.find(attrs={"role": "main"})
                    or soup.find("article")
                    or soup.body
                )
                if main:
                    for tag in main.find_all(
                        ["nav", "header", "footer", "script", "style", "form", "aside"]
                    ):
                        tag.decompose()
                    parts = []
                    for el in main.find_all(["p", "li", "h2", "h3", "h4"]):
                        t = el.get_text(" ", strip=True)
                        if len(t) > 20:
                            parts.append(t)
                    body_text = re.sub(r"\s+", " ", " ".join(parts)).strip()
                    if len(body_text) > len(abstract):
                        abstract = body_text
        except Exception as exc:
            print(f"[{_SITE_ID}] BS4 fallback error for {url}: {exc}")
        # Last-resort: meta description tag
        if len(abstract) < _MIN_ABSTRACT:
            mm = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]+)"', raw)
            if mm and len(mm.group(1)) > len(abstract):
                abstract = mm.group(1).strip()

    # --- PDF links (prefer ansto.gov.au; fall back to any .pdf) ---
    pdf_url = ""
    all_pdfs = re.findall(r'href="([^"]+\.pdf[^"]*)"', raw, re.I)
    ansto_pdfs = [
        p for p in all_pdfs
        if "ansto.gov.au" in p or p.startswith("/sites/default/files/")
    ]
    candidate = ansto_pdfs[0] if ansto_pdfs else (all_pdfs[0] if all_pdfs else "")
    if candidate:
        pdf_url = candidate if candidate.startswith("http") else _BASE + candidate

    # --- Fallback date from PDF path (e.g. /2020-05/filename.pdf) ---
    if not published_date and pdf_url:
        dm = re.search(r"/(\d{4}-\d{2})/", pdf_url)
        if dm:
            published_date = dm.group(1) + "-01"

    return {
        "abstract": abstract,
        "pdf_url": pdf_url,
        "published_date": published_date,
        "node_id": node_id,
        "original_filename": _original_filename(pdf_url),
    }


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class AnstoGovAuSearchCrawler(BaseCrawler):
    """Crawler for ANSTO gov.au full-text search (query=pdf)."""

    site_id = _SITE_ID
    site_name = "Custom: ansto-gov-au-search"
    base_url = _BASE

    def crawl(self, limit=None):
        """Paginate search results and save documents with abstracts."""
        saved = 0
        seen_urls: set[str] = set()
        crawl_start = time.time()
        limit_display = str(limit) if limit is not None else "∞"

        for page_num in range(_MAX_PAGES):
            # --- wall-clock budget ---
            if time.time() - crawl_start > _MAX_WALL:
                print(f"[{_SITE_ID}] 25-minute wall budget reached at page {page_num}. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page_num % 10 == 0:
                print(f"[{_SITE_ID}] page {page_num}: saved {saved}/{limit_display}")

            items = _fetch_search_page(page_num)
            if not items:
                print(f"[{_SITE_ID}] page {page_num}: no results returned. Stopping.")
                break

            # URL deduplication — detect pagination loops
            new_items = [it for it in items if it["url"] not in seen_urls]
            if not new_items:
                print(f"[{_SITE_ID}] page {page_num}: all items already seen (loop). Stopping.")
                break
            for it in new_items:
                seen_urls.add(it["url"])

            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                time.sleep(self._delay)

                try:
                    detail = _fetch_detail(item["url"])
                    if not detail:
                        print(f"[{_SITE_ID}] item failed (no detail): {item['url']}")
                        continue

                    abstract = detail.get("abstract", "")
                    if len(abstract) < _MIN_ABSTRACT:
                        print(
                            f"[{_SITE_ID}] skip (abstract {len(abstract)} chars): "
                            f"{item['url']}"
                        )
                        continue

                    node_id = detail.get("node_id", "")
                    # external_id: prefer numeric node ID, fall back to URL slug
                    external_id = node_id if node_id else (
                        item["url"].replace(_BASE, "").strip("/") or item["url"]
                    )
                    pdf_url = detail.get("pdf_url") or None
                    published_date = detail.get("published_date") or None
                    orig_fname = detail.get("original_filename") or None

                    metadata = {
                        "node_id": node_id,
                        "search_query": _QUERY,
                        "originalFilename": orig_fname,
                    }

                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": node_id or None,
                        "title": item["title"],
                        "abstract": abstract[:5000],
                        "url": item["url"],
                        "pdf_url": pdf_url,
                        "published_date": published_date,
                        "listed_date": None,
                        "authors": None,
                        "publisher": (
                            "Australian Nuclear Science and Technology Organisation"
                        ),
                        "department": None,
                        "journal": None,
                        "keywords": None,
                        "category": None,
                        "doi": None,
                        "original_filename": orig_fname,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    })
                    saved += 1
                    print(
                        f"[{_SITE_ID}] saved {saved}/{limit_display}: "
                        f"{item['title'][:60]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {item['url']} failed: {exc}")
                    continue

        if page_num == _MAX_PAGES - 1:
            print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached.")

        print(f"[{_SITE_ID}] crawl complete: {saved} saved.")
        return saved
