# -*- coding: utf-8 -*-
"""U.S. Department of Energy document search crawler.

Starting URL: https://www.energy.gov/search?page=0&f[0]=bundle_alias:Document
API: https://www.energy.gov/api/v1/search?f[0]=bundle_alias:Document&page=N
  - 10 results/page, up to 200 total (pages 0-19)
  - API repeats last page when going past the end (detected via currentPage mismatch)
Detail pages: body text in .field--name-field-text, PDF in .file--application-pdf
"""

import json
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_SITE_ID = "energy-gov-search"
_BASE = "https://www.energy.gov"
_API_URL = f"{_BASE}/api/v1/search"
_FILTER = "f%5B0%5D=bundle_alias%3ADocument"
_MIN_ABSTRACT = 100
_MAX_PAGES = 200
_MAX_WALL = 25 * 60  # 25-minute wall-clock budget


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _curl_get(url: str, *, timeout: int = 30, retries: int = 3) -> str | None:
    """Fetch URL via curl with TLS-max 1.3 and retry/backoff. Returns text or None."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "-H", "Accept: text/html,application/xhtml+xml,application/json,*/*;q=0.8",
        "-H", "Accept-Language: en-US,en;q=0.9",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 15)
            raw = result.stdout.decode("utf-8", errors="replace")
            if raw.strip():
                return raw
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(
                    f"[{_SITE_ID}] empty response for {url}, "
                    f"retry {attempt + 1}/{retries} in {wait}s"
                )
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(
                    f"[{_SITE_ID}] curl error: {exc}, "
                    f"retry {attempt + 1}/{retries} in {wait}s"
                )
                time.sleep(wait)
            else:
                print(f"[{_SITE_ID}] curl failed after {retries} attempts for {url}: {exc}")
    return None


# ---------------------------------------------------------------------------
# HTML parsing helper
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


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

_MONTHS = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "jun": "06", "jul": "07", "aug": "08", "sep": "09",
    "oct": "10", "nov": "11", "dec": "12",
}


def _parse_date(raw: str) -> str:
    """Convert 'May 11, 2026' or 'May 2026' → 'YYYY-MM-DD', or '' on failure."""
    if not raw:
        return ""
    raw = raw.strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", raw):
        return raw[:10]
    # "Month DD, YYYY"
    m = re.match(r"(\w+)\s+(\d{1,2}),\s*(\d{4})", raw)
    if m:
        mon = _MONTHS.get(m.group(1).lower(), "")
        if mon:
            return f"{m.group(3)}-{mon}-{int(m.group(2)):02d}"
    # "Month YYYY"
    m = re.match(r"(\w+)\s+(\d{4})", raw)
    if m:
        mon = _MONTHS.get(m.group(1).lower(), "")
        if mon:
            return f"{m.group(2)}-{mon}-01"
    # datetime fallback
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _original_filename(pdf_url: str) -> str:
    """Extract and URL-decode the filename from a PDF URL."""
    if not pdf_url:
        return ""
    try:
        path = urllib.parse.urlparse(pdf_url).path
        name = urllib.parse.unquote(path.split("/")[-1])
        return name if name.lower().endswith(".pdf") else ""
    except Exception:
        return ""


def _unescape(text: str) -> str:
    """Unescape common HTML entities."""
    return (
        text.replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
            .replace("&#39;", "'")
            .replace("&nbsp;", " ")
    )


# ---------------------------------------------------------------------------
# List page (JSON API)
# ---------------------------------------------------------------------------

def _fetch_list_page(page_num: int) -> tuple[list[dict], int]:
    """Fetch one API page of Document results.

    Returns (items, total_count).  items is a list of dicts with keys:
    title, url, date, office, article_type, node_id.
    Returns ([], 0) on error or pagination-end.
    """
    url = f"{_API_URL}?{_FILTER}&page={page_num}"
    raw = _curl_get(url)
    if not raw:
        return [], 0

    try:
        data = json.loads(raw)
    except Exception as exc:
        print(f"[{_SITE_ID}] JSON parse error on page {page_num}: {exc}")
        return [], 0

    pager = data.get("meta", {}).get("pager", {})
    current_page = pager.get("currentPage", page_num)
    total = data.get("meta", {}).get("totalResultCount", 0)

    # The API repeats the last page when going out of bounds.
    if page_num > 0 and current_page < page_num:
        return [], total

    rows = data.get("rows", [])
    items: list[dict] = []
    for row in rows:
        title_html = row.get("title", "")
        m = re.search(r'href="(/[^"]+)"', title_html)
        if not m:
            continue
        path = m.group(1)
        title = _unescape(row.get("titleUnion", "") or re.sub(r"<[^>]+>", "", title_html).strip())

        node_id = ""
        nm = re.search(r"node/(\d+)", row.get("id", ""))
        if nm:
            node_id = nm.group(1)

        items.append({
            "title": title,
            "url": _BASE + path,
            "date": row.get("date", ""),
            "office": _unescape(row.get("offices", "") or ""),
            "article_type": row.get("articleType", ""),
            "node_id": node_id,
        })

    return items, total


# ---------------------------------------------------------------------------
# Detail page
# ---------------------------------------------------------------------------

def _fetch_detail(url: str) -> dict:
    """Fetch a content detail page and extract abstract, pdf_url, published_date.

    Returns dict with keys: abstract, pdf_url, published_date, original_filename.
    Returns {} on total fetch failure.
    """
    raw = _curl_get(url)
    if not raw:
        return {}

    abstract = ""

    # Strategy 1: regex on field--name-field-text (main body text div)
    m = re.search(
        r'class="[^"]*field--name-field-text[^"]*"[^>]*>([\s\S]{10,8000}?)</div>\s*</div>',
        raw, re.IGNORECASE,
    )
    if m:
        text = re.sub(r"<[^>]+>", " ", m.group(1))
        text = _unescape(re.sub(r"\s+", " ", text).strip())
        if len(text) > len(abstract):
            abstract = text

    # Strategy 2: BS4 — main content area
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

                    # Try the specific body field first
                    field_div = main.find(class_=re.compile(r"field--name-field-text"))
                    if field_div:
                        text = _unescape(
                            re.sub(r"\s+", " ", field_div.get_text(" ", strip=True)).strip()
                        )
                        if len(text) > len(abstract):
                            abstract = text

                    # Fallback: collect all meaningful paragraphs
                    if len(abstract) < _MIN_ABSTRACT:
                        article = main.find("article") or main
                        parts = []
                        for el in article.find_all(["p", "li"]):
                            t = el.get_text(" ", strip=True)
                            if len(t) > 30:
                                parts.append(t)
                        body_text = _unescape(re.sub(r"\s+", " ", " ".join(parts)).strip())
                        if len(body_text) > len(abstract):
                            abstract = body_text
        except Exception as exc:
            print(f"[{_SITE_ID}] BS4 error for {url}: {exc}")

    # Strategy 3: meta description fallback
    if len(abstract) < _MIN_ABSTRACT:
        mm = re.search(
            r'<meta[^>]+name="description"[^>]+content="([^"]+)"', raw, re.IGNORECASE
        )
        if mm and len(mm.group(1)) > len(abstract):
            abstract = _unescape(mm.group(1).strip())

    # Published date from display-date span
    published_date = ""
    dm = re.search(
        r'<span[^>]*class="[^"]*display-date[^"]*"[^>]*>([\w\s,]+)</span>', raw
    )
    if dm:
        published_date = _parse_date(dm.group(1).strip())

    # PDF links — prefer /sites/default/files/
    pdf_url = ""
    all_pdfs = re.findall(r'href="([^"]+\.pdf[^"]*)"', raw, re.IGNORECASE)
    energy_pdfs = [
        p for p in all_pdfs
        if p.startswith("/sites/default/files/") or "energy.gov" in p
    ]
    candidate = energy_pdfs[0] if energy_pdfs else (all_pdfs[0] if all_pdfs else "")
    if candidate:
        pdf_url = candidate if candidate.startswith("http") else _BASE + candidate

    # Fallback date from PDF path (e.g. /2026-05/filename.pdf)
    if not published_date and pdf_url:
        dm2 = re.search(r"/(\d{4}-\d{2})/", pdf_url)
        if dm2:
            published_date = dm2.group(1) + "-01"

    return {
        "abstract": abstract,
        "pdf_url": pdf_url,
        "published_date": published_date,
        "original_filename": _original_filename(pdf_url),
    }


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class EnergyGovSearchCrawler(BaseCrawler):
    """Crawler for US DOE energy.gov document search (bundle_alias=Document)."""

    site_id = "energy-gov-search"
    site_name = "Custom: energy-gov-search"
    base_url = "https://www.energy.gov"

    def crawl(self, limit=None):
        """Paginate the API and save documents with ≥100-char abstracts."""
        saved = 0
        seen_urls: set[str] = set()
        crawl_start = time.time()
        limit_display = str(limit) if limit is not None else "∞"

        for page_num in range(_MAX_PAGES):
            if time.time() - crawl_start > _MAX_WALL:
                print(
                    f"[{_SITE_ID}] 25-minute wall budget reached at page {page_num}. Stopping."
                )
                break

            if limit is not None and saved >= limit:
                break

            if page_num % 10 == 0:
                print(f"[{_SITE_ID}] page {page_num}: saved {saved}/{limit_display}")

            items, _total = _fetch_list_page(page_num)

            if not items:
                print(f"[{_SITE_ID}] page {page_num}: no results returned. Stopping.")
                break

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

                    node_id = item["node_id"]
                    external_id = (
                        node_id if node_id
                        else item["url"].replace(_BASE, "").strip("/")
                    )

                    listed_date = _parse_date(item["date"])
                    published_date = detail.get("published_date") or listed_date or None
                    pdf_url = detail.get("pdf_url") or None
                    orig_fname = detail.get("original_filename") or None

                    metadata = {
                        "node_id": node_id,
                        "article_type": item.get("article_type", ""),
                        "posted_date": item["date"],
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
                        "posted_date": listed_date or None,
                        "authors": None,
                        "publisher": item["office"] or None,
                        "department": None,
                        "journal": None,
                        "keywords": None,
                        "category": item.get("article_type") or None,
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
