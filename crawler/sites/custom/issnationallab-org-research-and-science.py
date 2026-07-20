# -*- coding: utf-8 -*-
"""Crawler for ISS National Lab Research Reports.

Strategy:
  1. WP REST API: GET /wp-json/wp/v2/pages?parent=41447&per_page=100
     Yields all research-report child pages (~12 total, all on page 1).
  2. For each page item, fetch the rendered detail-page HTML.
     - Extract abstract text from content paragraphs.
     - Extract PDF download link (href matching /download/<id>/).
     - Fallback: if HTML abstract < 100 chars, query DLM REST API for
       content.rendered on the linked download ID.
  3. Save via _save_paper().
"""

from __future__ import annotations

import json
import re
import time

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_SITE_ID = "issnationallab-org-research-and-science"
_API_URL = "https://issnationallab.org/wp-json/wp/v2/pages"
_DLM_API_URL = "https://issnationallab.org/wp-json/wp/v2/dlm_download"
_PARENT_PAGE_ID = 41447
_ITEMS_PER_PAGE = 100
_MAX_PAGES = 200
_WALL_CLOCK_BUDGET = 25 * 60  # seconds
_MIN_ABSTRACT_LEN = 100        # skip items with abstract shorter than this
_ITEM_SLEEP = 1.0              # seconds between detail-page fetches
_PAGE_SLEEP = 0.5              # seconds between listing-page fetches
_BACKOFF = (1, 3, 9)           # retry wait times


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str) -> BeautifulSoup | None:
    """html5lib → lxml → html.parser fallback."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _strip_html(html: str) -> str:
    """Strip HTML tags; regex fallback if BS4 fails."""
    if not html:
        return ""
    try:
        soup = _make_soup(html)
        if soup is not None:
            return soup.get_text(separator=" ", strip=True)
    except Exception:
        pass
    return re.sub(r"<[^>]+>", " ", html).strip()


def _extract_abstract(html: str) -> str:
    """Extract abstract/description text from a detail-page HTML string.

    Removes chrome (nav, header, footer) then collects all <p> tags from
    the full body with >= 50 chars, skipping navigation fragments.
    Falls back to broader element search if paragraphs yield too little.
    """
    soup = _make_soup(html)
    if soup is None:
        return ""

    for tag in soup.find_all(["script", "style", "nav", "header", "footer",
                               "noscript", "aside"]):
        tag.decompose()

    body = soup.find("body") or soup

    # Skip patterns that indicate navigation / chrome text
    _SKIP = re.compile(
        r"^(«|Back to|Sign up|You'll receive|We'll keep|Download this|"
        r"Subscribe|Cookie|Privacy|Copyright|All rights)",
        re.IGNORECASE,
    )

    seen: set[str] = set()
    texts: list[str] = []

    # Primary pass: <p> tags with >= 50 chars
    for elem in body.find_all("p"):
        text = re.sub(r"\s+", " ", elem.get_text(separator=" ", strip=True)).strip()
        if len(text) >= 50 and text not in seen and not _SKIP.match(text):
            seen.add(text)
            texts.append(text)

    if texts:
        return " ".join(texts)

    # Fallback: any block element >= 30 chars
    for elem in body.find_all(["p", "li", "dd", "blockquote"]):
        text = re.sub(r"\s+", " ", elem.get_text(separator=" ", strip=True)).strip()
        if len(text) >= 30 and text not in seen and not _SKIP.match(text):
            seen.add(text)
            texts.append(text)

    if texts:
        return " ".join(texts)

    # Last resort: full body text
    full = re.sub(r"\s+", " ", body.get_text(separator=" ", strip=True)).strip()
    return full


def _extract_pdf_url(html: str) -> str:
    """Return the first DLM download URL found in the page HTML."""
    m = re.search(
        r'href=["\']?(https?://issnationallab\.org/download/\d+/[^"\'>\s]*)',
        html,
    )
    if m:
        return m.group(1).split("?")[0].rstrip("/") + "/"
    m = re.search(r'href=["\']?(/download/(\d+)/[^"\'>\s]*)', html)
    if m:
        path = m.group(1).split("?")[0].rstrip("/") + "/"
        return f"https://issnationallab.org{path}"
    return ""


def _extract_dlm_id(html: str) -> str | None:
    """Extract DLM download numeric ID from page HTML, or None."""
    m = re.search(r'/download/(\d+)/', html)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class ISSNationalLabResearchAndScienceCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: issnationallab-org-research-and-science"
    base_url = "https://issnationallab.org"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Cloudflare proxies this site; avoid Brotli (requires extra package).
        self._session.headers.update({"Accept-Encoding": "gzip, deflate"})

    # -----------------------------------------------------------------------
    # Network helpers
    # -----------------------------------------------------------------------

    def _api_get(self, url: str, params: dict | None = None) -> list | dict | None:
        """GET a WP REST API endpoint with retries.  Returns parsed JSON or None."""
        for attempt, wait in enumerate(_BACKOFF):
            try:
                resp = self._session.get(url, params=params, timeout=30)
                if resp.status_code in (400, 404):
                    return []
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                print(
                    f"[{_SITE_ID}] API error (attempt {attempt+1}/{len(_BACKOFF)}) "
                    f"{url}: {exc}"
                )
                if attempt < len(_BACKOFF) - 1:
                    time.sleep(wait)
        return None

    def _fetch_html(self, url: str) -> str | None:
        """GET a page, return decoded HTML or None after retries."""
        for attempt, wait in enumerate(_BACKOFF):
            try:
                resp = self._session.get(url, timeout=30)
                resp.raise_for_status()
                try:
                    return resp.text
                except Exception:
                    return resp.content.decode("utf-8", errors="replace")
            except Exception as exc:
                print(
                    f"[{_SITE_ID}] HTML fetch error "
                    f"(attempt {attempt+1}/{len(_BACKOFF)}) {url}: {exc}"
                )
                if attempt < len(_BACKOFF) - 1:
                    time.sleep(wait)
        return None

    def _dlm_content(self, dlm_id: str) -> str:
        """Fetch content.rendered for a DLM download item; strip HTML."""
        data = self._api_get(
            f"{_DLM_API_URL}/{dlm_id}",
            params={"_fields": "content"},
        )
        if not data or not isinstance(data, dict):
            return ""
        content_html = data.get("content", {})
        if isinstance(content_html, dict):
            content_html = content_html.get("rendered", "")
        return _strip_html(content_html).strip()

    # -----------------------------------------------------------------------
    # Listing page
    # -----------------------------------------------------------------------

    def _fetch_list_page(self, page: int) -> list | None:
        """Return one page of WP child-pages under _PARENT_PAGE_ID, or None on error."""
        data = self._api_get(
            _API_URL,
            params={
                "parent": _PARENT_PAGE_ID,
                "per_page": _ITEMS_PER_PAGE,
                "page": page,
                "status": "publish",
                "_fields": "id,date,link,title,slug",
            },
        )
        if data is None:
            return None
        if isinstance(data, list):
            return data
        return []

    # -----------------------------------------------------------------------
    # Main crawl
    # -----------------------------------------------------------------------

    def crawl(self, limit=None):
        limit_str = str(limit) if limit is not None else "∞"
        print(f"[{_SITE_ID}] starting crawl, limit={limit_str}")

        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()

        page = 1
        while True:
            # --- stop conditions ---
            if limit is not None and saved >= limit:
                break

            elapsed = time.time() - start_time
            if elapsed > _WALL_CLOCK_BUDGET:
                print(
                    f"[{_SITE_ID}] 25-minute budget reached at page {page}, "
                    f"stopping early (saved {saved})"
                )
                break

            if page > _MAX_PAGES:
                print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached, stopping")
                break

            if page > 1 and page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_str}")

            # --- fetch listing ---
            items = self._fetch_list_page(page)
            if items is None:
                print(f"[{_SITE_ID}] page {page}: persistent failure, stopping")
                break
            if not items:
                print(f"[{_SITE_ID}] page {page}: no results, end of pagination")
                break

            # --- process items ---
            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                try:
                    post_id = item.get("id")
                    url = item.get("link", "")
                    if not url:
                        continue

                    if url in seen_urls:
                        continue
                    seen_urls.add(url)

                    # Title
                    title_raw = item.get("title", {})
                    if isinstance(title_raw, dict):
                        title_raw = title_raw.get("rendered", "")
                    title = _strip_html(title_raw).strip()
                    if not title:
                        print(f"[{_SITE_ID}] item {post_id}: no title, skipping")
                        continue

                    # Published date (ISO date from API)
                    date_raw = item.get("date", "")
                    published_date = date_raw[:10] if date_raw else ""

                    # Fetch rendered detail page
                    time.sleep(_ITEM_SLEEP)
                    html = self._fetch_html(url)
                    if not html:
                        print(f"[{_SITE_ID}] item {post_id}: detail fetch failed, skipping")
                        continue

                    # Abstract
                    abstract = _extract_abstract(html)

                    # If still too short, try DLM content as fallback
                    if len(abstract) < _MIN_ABSTRACT_LEN:
                        dlm_id = _extract_dlm_id(html)
                        if dlm_id:
                            dlm_text = self._dlm_content(dlm_id)
                            if len(dlm_text) > len(abstract):
                                abstract = dlm_text

                    if len(abstract) < _MIN_ABSTRACT_LEN:
                        print(
                            f"[{_SITE_ID}] item {post_id} ({title[:50]}): "
                            f"abstract too short ({len(abstract)} chars), skipping"
                        )
                        continue

                    pdf_url = _extract_pdf_url(html)
                    dlm_id = _extract_dlm_id(html)

                    paper = {
                        "site_id": _SITE_ID,
                        "external_id": str(post_id),
                        "title": title,
                        "authors": json.dumps([]),
                        "abstract": abstract,
                        "category": "Research Reports",
                        "keywords": json.dumps([]),
                        "published_date": published_date,
                        "url": url,
                        "pdf_url": pdf_url,
                        "doi": "",
                        "department": "ISS National Lab",
                        "metadata": json.dumps({
                            "post_id": post_id,
                            "slug": item.get("slug", ""),
                            "dlm_id": dlm_id,
                        }),
                    }

                    self._save_paper(paper)
                    saved += 1
                    new_on_page += 1
                    print(f"[{_SITE_ID}] saved [{saved}]: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {item.get('id', '?')} failed: {exc}")
                    continue

            # Silent-looping paginator detection
            if new_on_page == 0 and len(items) > 0:
                print(
                    f"[{_SITE_ID}] page {page}: all {len(items)} items already seen "
                    "(dedup loop detected), stopping"
                )
                break

            time.sleep(_PAGE_SLEEP)
            page += 1

        print(f"[{_SITE_ID}] crawl complete: saved {saved} records")
        return saved
