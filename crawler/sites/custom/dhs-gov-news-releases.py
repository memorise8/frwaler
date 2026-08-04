# -*- coding: utf-8 -*-
"""DHS.gov press releases crawler.

Starting URL: https://www.dhs.gov/news-releases/press-releases
Pagination: ?page=N  (0-indexed, ~99 pages, 10 items/page)

dhs.gov is behind Akamai which blocks datacenter IPs/plain-curl clients
regardless of User-Agent header — it fingerprints at the TLS/HTTP2 layer.
curl_cffi (Chrome TLS impersonation) passes and is far faster than
launching a full headless browser per request; Playwright (headless
Chromium) is kept as a fallback in case curl_cffi's fingerprint ever
gets flagged too.
"""

import json
import os
import re
import sys
import time
from datetime import datetime

# Absolute import — required because spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

try:
    from curl_cffi import requests as _cffi_requests
    _CFFI_AVAILABLE = True
except ImportError:
    _CFFI_AVAILABLE = False

_BASE = "https://www.dhs.gov"
_LIST_URL = f"{_BASE}/news-releases/press-releases"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))


def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _fetch_cffi(url: str, *, retries: int = 3) -> str | None:
    """Fetch URL via curl_cffi (Chrome TLS impersonation) with backoff retry."""
    if not _CFFI_AVAILABLE:
        return None
    backoff = [1, 3, 9]
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    for attempt in range(retries):
        try:
            r = _cffi_requests.get(url, headers=headers, impersonate="chrome124", timeout=30)
            if r.status_code < 400 and r.text and len(r.text) > 500:
                return r.text
            print(f"[dhs-gov-news-releases] curl_cffi short/empty response "
                  f"(HTTP {r.status_code}, attempt {attempt + 1}/{retries}) for {url}")
        except Exception as exc:
            print(f"[dhs-gov-news-releases] curl_cffi error "
                  f"(attempt {attempt + 1}/{retries}) for {url}: {exc}")
        if attempt < retries - 1:
            time.sleep(backoff[attempt])
    return None


def _fetch(url: str, *, retries: int = 3) -> str | None:
    """Fetch a URL, preferring curl_cffi (fast) with a Playwright fallback."""
    html = _fetch_cffi(url, retries=retries)
    if html:
        return html
    return _fetch_playwright(url, retries=retries)


def _fetch_playwright(url: str, *, retries: int = 3) -> str | None:
    """Fetch URL via Playwright with exponential-backoff retry."""
    try:
        from crawler.playwright_fetcher import fetch_html
    except ImportError:
        print("[dhs-gov-news-releases] playwright_fetcher not available", file=sys.stderr)
        return None

    backoff = [1, 3, 9]
    for attempt in range(retries):
        try:
            html = fetch_html(url, timeout_seconds=35, extra_wait_seconds=2.0)
            if html and len(html) > 500:
                return html
            print(f"[dhs-gov-news-releases] short/empty response "
                  f"(attempt {attempt + 1}/{retries}) for {url}")
        except Exception as exc:
            print(f"[dhs-gov-news-releases] Playwright error "
                  f"(attempt {attempt + 1}/{retries}) for {url}: {exc}")
        if attempt < retries - 1:
            time.sleep(backoff[attempt])
    print(f"[dhs-gov-news-releases] All {retries} attempts failed for {url}")
    return None


def _extract_list_links(soup) -> list:
    """Return deduplicated article URLs from a list page."""
    links = []
    seen = set()
    pattern = re.compile(r"/news/\d{4}/\d{2}/\d{2}/")
    for a in soup.find_all("a", href=pattern):
        href = a.get("href", "")
        if not href or href in seen:
            continue
        seen.add(href)
        if href.startswith("/"):
            href = f"{_BASE}{href}"
        links.append(href)
    return links


def _parse_date(raw: str) -> str:
    """Convert 'May 13, 2026' (or similar) → 'YYYY-MM-DD'."""
    if not raw:
        return ""
    raw = re.sub(r"\s+", " ", raw.replace("\xa0", " ")).strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return m.group(0)
    return ""


def _extract_article_fields(soup, url: str) -> dict:
    """Return a dict of fields parsed from an individual press release page."""
    # Title
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""

    # Date
    date_el = (soup.find(class_="news-release-date-value")
               or soup.find(class_="block-news-release-date"))
    raw_date = date_el.get_text(strip=True) if date_el else ""
    published_date = _parse_date(raw_date)

    # Fallback: parse date from URL path /news/YYYY/MM/DD/
    if not published_date:
        dm = re.search(r"/news/(\d{4})/(\d{2})/(\d{2})/", url)
        if dm:
            published_date = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}"

    # Abstract — find <article>, strip inline style/script, then get_text
    abstract = ""
    article_el = soup.find("article")
    if article_el:
        for dead in article_el.find_all(["style", "script", "noscript"]):
            dead.decompose()
        text = article_el.get_text(separator="\n", strip=True)
        abstract = re.sub(r"\n{3,}", "\n\n", text).strip()

    # Fallback: meta description
    if not abstract or len(abstract) < 50:
        meta = soup.find("meta", attrs={"name": "description"})
        if meta:
            abstract = (meta.get("content") or "").strip()

    # Meta description (preserve separately for metadata)
    meta_el = soup.find("meta", attrs={"name": "description"})
    meta_desc = (meta_el.get("content") or "").strip() if meta_el else ""

    # external_id = URL slug (last path component)
    sm = re.search(r"/news/\d{4}/\d{2}/\d{2}/([^/?#]+)", url)
    slug = sm.group(1) if sm else url

    # post_number = YYYYMMDD-slug (string; sorts chronologically)
    post_number = None
    dm2 = re.search(r"/news/(\d{4})/(\d{2})/(\d{2})/", url)
    if dm2:
        post_number = f"{dm2.group(1)}{dm2.group(2)}{dm2.group(3)}-{slug}"

    return {
        "external_id": slug,
        "post_number": post_number,
        "title": title,
        "abstract": abstract,
        "published_date": published_date,
        "listed_date": published_date,
        "url": url,
        "publisher": "U.S. Department of Homeland Security",
        "category": "Press Release",
        "metadata": json.dumps(
            {"raw_date": raw_date, "meta_description": meta_desc, "slug": slug},
            ensure_ascii=False,
        ),
    }


class DHSNewsReleasesCrawler(BaseCrawler):
    site_id = "dhs-gov-news-releases"
    site_name = "Custom: dhs-gov-news-releases"
    base_url = "https://www.dhs.gov"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        page = 0
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "inf"

        while True:
            # ── termination guards ────────────────────────────────────
            if limit is not None and saved >= limit:
                break
            if page >= _MAX_PAGES:
                print(f"[dhs-gov-news-releases] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break
            elapsed = time.time() - start_time
            if elapsed > _MAX_WALL_SECONDS:
                print(f"[dhs-gov-news-releases] Wall-clock budget exceeded ({elapsed:.0f}s). Stopping.")
                break

            # ── fetch list page ───────────────────────────────────────
            list_url = f"{_LIST_URL}?page={page}"
            html = _fetch(list_url)
            if not html:
                print(f"[dhs-gov-news-releases] list page {page}: fetch failed. Stopping.")
                break

            try:
                soup = _make_soup(html)
            except Exception as exc:
                print(f"[dhs-gov-news-releases] list page {page}: parse error: {exc}. Stopping.")
                break
            if soup is None:
                print(f"[dhs-gov-news-releases] list page {page}: no parser available. Stopping.")
                break

            article_links = _extract_list_links(soup)
            if not article_links:
                print(f"[dhs-gov-news-releases] list page {page}: no article links found. Done.")
                break

            new_links = [l for l in article_links if l not in seen_urls]
            if not new_links:
                print(f"[dhs-gov-news-releases] list page {page}: all links already seen. Done.")
                break

            seen_urls.update(article_links)

            if page > 0 and page % 10 == 0:
                print(f"[dhs-gov-news-releases] page {page}: saved {saved}/{limit_str}")

            # ── fetch each article ────────────────────────────────────
            for article_url in new_links:
                if limit is not None and saved >= limit:
                    break

                elapsed = time.time() - start_time
                if elapsed > _MAX_WALL_SECONDS:
                    print(f"[dhs-gov-news-releases] Wall-clock budget exceeded. Stopping.")
                    break

                try:
                    time.sleep(self._delay)
                    article_html = _fetch(article_url)
                    if not article_html:
                        print(f"[dhs-gov-news-releases] item {article_url} failed: empty response; continue")
                        continue

                    try:
                        article_soup = _make_soup(article_html)
                    except Exception as exc:
                        print(f"[dhs-gov-news-releases] item {article_url} failed: {exc}; continue")
                        continue
                    if article_soup is None:
                        print(f"[dhs-gov-news-releases] item {article_url} failed: no parser; continue")
                        continue

                    fields = _extract_article_fields(article_soup, article_url)

                    if not fields.get("abstract") or len(fields["abstract"]) < 50:
                        print(f"[dhs-gov-news-releases] Skipping {article_url}: "
                              f"abstract too short ({len(fields.get('abstract', ''))} chars)")
                        continue

                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": fields["external_id"],
                        "post_number": fields["post_number"],
                        "title": fields["title"],
                        "abstract": fields["abstract"],
                        "published_date": fields["published_date"],
                        "listed_date": fields["listed_date"],
                        "url": article_url,
                        "pdf_url": None,
                        "publisher": fields["publisher"],
                        "category": fields["category"],
                        "keywords": None,
                        "doi": None,
                        "original_filename": None,
                        "metadata": fields["metadata"],
                    })
                    saved += 1
                    print(f"[dhs-gov-news-releases] Saved {saved}/{limit_str}: "
                          f"{fields['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[dhs-gov-news-releases] item {article_url} failed: {exc}; continue")
                    continue

            page += 1

        print(f"[dhs-gov-news-releases] Done. Total saved: {saved}")
        return saved
