# -*- coding: utf-8 -*-
"""NZ Ministry of Defence Publications crawler.

Starting URL: https://www.defence.govt.nz/publications/doSearch/
Pagination:   GET ?start=N  (10 items/page, ~413 total results)
Detail pages: individual publication pages with full abstract.
"""

import json
import re
import subprocess
import sys
import time
import urllib.parse

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_SITE_ID = "defence-govt-nz-publications"
_BASE = "https://www.defence.govt.nz"
_LIST_URL = f"{_BASE}/publications/doSearch/"
_PAGE_SIZE = 10
_MIN_ABSTRACT = 100
_MAX_PAGES = 200
_MAX_WALL = 25 * 60  # 25-minute wall-clock budget

_MONTH_MAP = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _curl_get(url, *, timeout=30, retries=3):
    """Fetch url via curl; return decoded text or None on failure."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: en-NZ,en;q=0.9",
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

def _make_soup(html):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _parse_date(raw):
    """Convert 'DD Mon YYYY' to 'YYYY-MM-DD', or return None."""
    if not raw:
        return None
    m = re.match(r"(\d{1,2})\s+(\w+)\s+(\d{4})", raw.strip())
    if not m:
        return None
    day, mon, year = m.group(1), m.group(2).lower()[:3], m.group(3)
    mo = _MONTH_MAP.get(mon)
    if not mo:
        return None
    return f"{year}-{mo}-{int(day):02d}"


def _slug_from_url(url):
    """Extract the last path segment (slug) from a URL."""
    try:
        path = urllib.parse.urlparse(url).path
        return path.strip("/").split("/")[-1]
    except Exception:
        return url


def _original_filename(pdf_url):
    """Return URL-decoded filename from a PDF URL path, or None."""
    if not pdf_url:
        return None
    try:
        path = urllib.parse.urlparse(pdf_url).path
        name = urllib.parse.unquote(path.split("/")[-1])
        return name or None
    except Exception:
        return None


def _clean_text(text):
    """Normalise whitespace and strip HTML entities."""
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# List-page parser
# ---------------------------------------------------------------------------

def _fetch_list_page(start):
    """Fetch one listing page (start=N) and return list of item dicts."""
    url = f"{_LIST_URL}?start={start}"
    raw = _curl_get(url)
    if not raw:
        return []

    soup = _make_soup(raw)
    if not soup:
        return []

    items = []
    for block in soup.find_all("div", class_="search-result"):
        try:
            # Date from list
            date_raw = ""
            date_el = block.find(class_="search-result__date")
            if date_el:
                b_tag = date_el.find("b")
                if b_tag:
                    date_raw = b_tag.get_text(strip=True)

            # Title + detail URL
            text_el = block.find(class_="search-result__text")
            if not text_el:
                continue
            a_tag = text_el.find("a")
            if not a_tag:
                continue
            title = _clean_text(a_tag.get_text(strip=True))
            if not title:
                continue
            rel_url = a_tag.get("href", "")
            if not rel_url:
                continue
            detail_url = (_BASE + rel_url) if rel_url.startswith("/") else rel_url

            # Short abstract from list (may be truncated)
            p_tag = text_el.find("p")
            abstract_short = _clean_text(p_tag.get_text(" ", strip=True)) if p_tag else ""

            # PDF URL from list
            pdf_url = None
            buttons_el = block.find(class_="search-result__buttons")
            if buttons_el:
                dl_a = buttons_el.find(
                    "a", class_=lambda c: c and "button--download" in c
                )
                if dl_a:
                    href = dl_a.get("href", "")
                    if href:
                        pdf_url = (_BASE + href) if href.startswith("/") else href

            items.append({
                "title": title,
                "date_raw": date_raw,
                "url": detail_url,
                "abstract_short": abstract_short,
                "pdf_url": pdf_url,
            })
        except Exception:
            continue

    return items


# ---------------------------------------------------------------------------
# Detail-page parser
# ---------------------------------------------------------------------------

def _fetch_detail(url):
    """Fetch a detail page and return metadata dict.

    Keys: abstract, published_date, category, pdf_url.
    Returns {} on total fetch failure.
    """
    raw = _curl_get(url)
    if not raw:
        return {}

    soup = _make_soup(raw)
    if not soup:
        return {}

    result = {}

    # Published date
    date_el = soup.find(attrs={"data-element": "publication-date"})
    if date_el:
        text = date_el.get_text(" ", strip=True)
        m = re.search(r"(\d{1,2}\s+\w+\s+\d{4})", text)
        result["published_date"] = _parse_date(m.group(1)) if m else None

    # Category
    cat_el = soup.find(attrs={"data-element": "publication-category"})
    if cat_el:
        text = cat_el.get_text(" ", strip=True)
        result["category"] = re.sub(r"^Category\s*[:\-]\s*", "", text, flags=re.I).strip()

    # Main summary — remove buttons/links before extracting text
    abstract_parts = []
    summary_el = soup.find(attrs={"data-element": "publication-summary"})
    if summary_el:
        for tag in summary_el.find_all(["a", "button"]):
            tag.decompose()
        text = _clean_text(summary_el.get_text(" ", strip=True))
        if text:
            abstract_parts.append(text)

    # Additional info (extends abstract if summary is short)
    add_el = soup.find(attrs={"data-element": "publication-additional-info"})
    if add_el:
        typo = add_el.find(class_="typography")
        if not typo:
            typo = add_el
        # Remove heading tags that just say "Additional info"
        for tag in typo.find_all(["h2", "h3", "h4"]):
            tag.decompose()
        add_text = _clean_text(typo.get_text(" ", strip=True))
        if add_text:
            abstract_parts.append(add_text)

    result["abstract"] = " ".join(abstract_parts).strip()

    # PDF URL from detail page (more reliable than list page)
    pdf_a = soup.find("a", attrs={"data-element": "publication-button"})
    if not pdf_a:
        pdf_a = soup.find("a", class_=lambda c: c and "button--download" in c)
    if pdf_a:
        href = pdf_a.get("href", "")
        if href:
            result["pdf_url"] = (_BASE + href) if href.startswith("/") else href

    return result


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class DefenceGovtNzPublicationsCrawler(BaseCrawler):
    """Crawler for NZ Ministry of Defence publications listing."""

    site_id = _SITE_ID
    site_name = "Custom: defence-govt-nz-publications"
    base_url = _BASE

    def crawl(self, limit=None):
        """Paginate listing and save publications with full abstracts."""
        saved = 0
        seen_urls: set = set()
        crawl_start = time.time()
        limit_display = str(limit) if limit is not None else "∞"

        for page_num in range(_MAX_PAGES):
            # Wall-clock budget
            if time.time() - crawl_start > _MAX_WALL:
                print(f"[{_SITE_ID}] 25-minute wall budget reached at page {page_num}. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            start = page_num * _PAGE_SIZE
            if page_num % 10 == 0:
                print(f"[{_SITE_ID}] page {page_num}: saved {saved}/{limit_display}")

            items = _fetch_list_page(start)
            if not items:
                print(f"[{_SITE_ID}] page {page_num}: no results returned. Stopping.")
                break

            # URL deduplication — detect pagination loop-back
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

                    # Build abstract: prefer detail page, fall back to list snippet
                    abstract = detail.get("abstract", "") or item.get("abstract_short", "")
                    abstract = _clean_text(abstract)

                    if len(abstract) < _MIN_ABSTRACT:
                        print(
                            f"[{_SITE_ID}] skip (abstract {len(abstract)} chars): "
                            f"{item['url']}"
                        )
                        continue

                    slug = _slug_from_url(item["url"])
                    published_date = (
                        detail.get("published_date")
                        or _parse_date(item.get("date_raw", ""))
                    )
                    category = detail.get("category") or None
                    pdf_url = detail.get("pdf_url") or item.get("pdf_url") or None
                    orig_fname = _original_filename(pdf_url)

                    metadata = {
                        "slug": slug,
                        "posted_date": item.get("date_raw") or None,
                        "category_raw": category,
                        "originalFilename": orig_fname,
                    }

                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": slug,
                        "post_number": slug,
                        "title": item["title"],
                        "abstract": abstract[:8000],
                        "url": item["url"],
                        "pdf_url": pdf_url,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "authors": None,
                        "publisher": "Ministry of Defence, New Zealand",
                        "department": None,
                        "journal": None,
                        "keywords": None,
                        "category": category,
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
