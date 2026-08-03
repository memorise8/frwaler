# -*- coding: utf-8 -*-
"""Crawler for Danish Ministry of Business — Udgivelser og aftaler.

Starting URL: https://www.em.dk/aktuelt/udgivelser-og-aftaler

Architecture:
1. Fetch the main index page to get all year/month sub-page paths from the
   left-side navigation tree (which fully expands on any page load).
2. For each month page, fetch its HTML and extract the GoBasic itemlist
   registration data: base64 context + HMAC hash + generator class name.
3. POST to [month_path]/proxy.gba with Content-Type "versus/callback" and
   the registered config to retrieve paginated item HTML fragments.
4. Parse each item's detail URL from the fragment (data-url attribute).
5. Fetch each detail page; extract title, date, rich-text abstract, PDF link,
   and category.
6. Persist via self._save_paper().
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from html import unescape

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_BASE_URL = "https://www.em.dk"
_INDEX_URL = _BASE_URL + "/aktuelt/udgivelser-og-aftaler"
_GENERATOR = "GoBasic.Presentation.Controls.ListHelper, GoBasic.Presentation"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))        # safety cap on pages-per-month
_WALL_MINUTES = 25      # total crawl wall-clock budget
_MIN_ABSTRACT = 50      # skip items whose abstract is shorter than this
_DETAIL_DELAY = 1.0     # seconds between detail page fetches
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _curl_get(url: str, retries: int = 3) -> str:
    """GET via curl (TLS-1.3, insecure). Returns UTF-8 text or '' on failure."""
    backoff = [1, 3, 9]
    for attempt in range(retries):
        if attempt > 0:
            time.sleep(backoff[attempt - 1])
        try:
            cmd = [
                "curl", "--tls-max", "1.3", "-skL",
                "--max-time", "30",
                "--user-agent", _UA,
                "-H", "Accept: text/html,*/*;q=0.9",
                "-H", "Accept-Language: da-DK,da;q=0.9,en;q=0.8",
                url,
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            if result.returncode == 0 and result.stdout:
                return result.stdout.decode("utf-8", errors="replace")
            print(
                f"[em-dk-aktuelt] curl GET exit {result.returncode} "
                f"(attempt {attempt+1}/{retries}) {url}"
            )
        except subprocess.TimeoutExpired:
            print(
                f"[em-dk-aktuelt] curl GET timeout "
                f"(attempt {attempt+1}/{retries}) {url}"
            )
        except Exception as exc:
            print(
                f"[em-dk-aktuelt] curl GET error "
                f"(attempt {attempt+1}/{retries}) {url}: {exc}"
            )
    return ""


def _curl_post_proxy(month_path: str, payload: dict, retries: int = 3) -> dict | None:
    """POST to [month_path]/proxy.gba with versus/callback content-type.

    Returns parsed JSON response dict, or None on failure.
    """
    url = _BASE_URL + month_path + "/proxy.gba"
    data = json.dumps(payload, ensure_ascii=False)
    backoff = [1, 3, 9]
    for attempt in range(retries):
        if attempt > 0:
            time.sleep(backoff[attempt - 1])
        try:
            cmd = [
                "curl", "--tls-max", "1.3", "-sk",
                "--max-time", "30",
                "-X", "POST",
                "-H", "Content-Type: versus/callback; charset=utf-8",
                "-H", "X-Cacheable: true",
                "-H", "X-Requested-With: XMLHttpRequest",
                "--user-agent", _UA,
                "-d", data,
                url,
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            if result.returncode == 0 and result.stdout:
                raw = result.stdout.decode("utf-8", errors="replace")
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    print(
                        f"[em-dk-aktuelt] proxy.gba JSON parse error "
                        f"(attempt {attempt+1}/{retries}): {raw[:200]}"
                    )
            else:
                print(
                    f"[em-dk-aktuelt] proxy.gba curl exit {result.returncode} "
                    f"(attempt {attempt+1}/{retries}) {url}"
                )
        except subprocess.TimeoutExpired:
            print(
                f"[em-dk-aktuelt] proxy.gba timeout "
                f"(attempt {attempt+1}/{retries}) {url}"
            )
        except Exception as exc:
            print(
                f"[em-dk-aktuelt] proxy.gba error "
                f"(attempt {attempt+1}/{retries}) {url}: {exc}"
            )
    return None


# ---------------------------------------------------------------------------
# HTML parse helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _parse_date_dk(raw: str) -> str:
    """Convert DD-MM-YYYY (Danish format) to ISO YYYY-MM-DD."""
    m = re.match(r"(\d{1,2})-(\d{1,2})-(\d{4})", raw.strip())
    if m:
        d, mo, y = m.groups()
        return f"{y}-{mo.zfill(2)}-{d.zfill(2)}"
    return raw.strip()


# ---------------------------------------------------------------------------
# GoBasic itemlist config extraction
# ---------------------------------------------------------------------------

def _extract_itemlist_config(html: str) -> dict | None:
    """Extract the JSON object from application.script.register('itemlist', {...}, ...)."""
    idx = html.find("register('itemlist',")
    if idx == -1:
        return None
    start = html.find("{", idx)
    if start == -1:
        return None
    # Walk forward counting braces to find the matching closing brace
    depth = 0
    for i in range(start, len(html)):
        ch = html[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                json_str = html[start : i + 1]
                try:
                    return json.loads(json_str)
                except json.JSONDecodeError as exc:
                    print(f"[em-dk-aktuelt] itemlist config JSON parse error: {exc}")
                    return None
    return None


# ---------------------------------------------------------------------------
# List-page item URL extraction
# ---------------------------------------------------------------------------

def _extract_item_urls(html_fragment: str) -> list[str]:
    """Return ordered list of unique detail-page URLs from GoBasic item HTML."""
    urls: list[str] = []
    seen: set = set()
    # Primary: data-url attribute on .item divs
    for m in re.finditer(r'data-url="(https?://[^"]+)"', html_fragment):
        u = m.group(1)
        if u not in seen:
            seen.add(u)
            urls.append(u)
    # Fallback: heading link hrefs
    if not urls:
        for m in re.finditer(
            r'class="heading"[^>]*>\s*<a\s+href="(https?://[^"]+)"', html_fragment
        ):
            u = m.group(1)
            if u not in seen:
                seen.add(u)
                urls.append(u)
    return urls


# ---------------------------------------------------------------------------
# Detail page parsing
# ---------------------------------------------------------------------------

def _parse_detail(url: str, html: str) -> dict | None:
    """Parse a publication detail page and return a paper dict, or None on failure."""
    try:
        soup = _make_soup(html)
    except Exception as exc:
        print(f"[em-dk-aktuelt] soup error for {url}: {exc}")
        soup = None

    # --- Title ---
    title = ""
    if soup is not None:
        try:
            h1 = soup.select_one("h1.heading")
            if h1:
                title = h1.get_text(strip=True)
        except Exception:
            pass
    if not title:
        m = re.search(r'<h1[^>]*class="heading"[^>]*>(.*?)</h1>', html, re.DOTALL)
        if m:
            title = _strip_tags(m.group(1))
    if not title:
        return None

    # --- Published date ---
    pub_date = ""
    if soup is not None:
        try:
            span = soup.select_one("span.date")
            if span:
                pub_date = _parse_date_dk(span.get_text(strip=True))
        except Exception:
            pass
    if not pub_date:
        m = re.search(r'<span[^>]*class="date"[^>]*>(.*?)</span>', html)
        if m:
            pub_date = _parse_date_dk(unescape(m.group(1)))

    # --- Abstract from rich-text div ---
    abstract = ""
    if soup is not None:
        try:
            rich = soup.select_one("div.rich-text")
            if rich:
                abstract = rich.get_text(separator=" ", strip=True)
        except Exception:
            pass
    if not abstract:
        m = re.search(
            r'<div[^>]*class="rich-text"[^>]*>(.*?)</div>', html, re.DOTALL
        )
        if m:
            abstract = _strip_tags(m.group(1))
    abstract = re.sub(r"\s+", " ", abstract).strip()

    # --- PDF URL ---
    pdf_url: str | None = None
    if soup is not None:
        try:
            btn = soup.select_one("div.group a.btn[href]")
            if btn:
                href = btn.get("href", "")
                if href:
                    if not href.startswith("http"):
                        href = _BASE_URL + href
                    pdf_url = href
        except Exception:
            pass
    if not pdf_url:
        m = re.search(r'href="(/Media/[^"]+\.pdf[^"]*)"', html, re.IGNORECASE)
        if m:
            pdf_url = _BASE_URL + m.group(1)

    # --- Original filename from PDF URL ---
    original_filename: str | None = None
    if pdf_url:
        path_part = pdf_url.split("?")[0]
        tail = urllib.parse.unquote(path_part.split("/")[-1])
        if tail:
            original_filename = tail

    # --- Category ---
    category = ""
    if soup is not None:
        try:
            lbl = soup.select_one("div.labels a span.label")
            if lbl:
                category = lbl.get_text(strip=True)
            else:
                lbl = soup.select_one("div.labels a span")
                if lbl:
                    category = lbl.get_text(strip=True)
        except Exception:
            pass
    if not category:
        m = re.search(r'<span class="label">(.*?)</span>', html)
        if m:
            category = unescape(m.group(1)).strip()

    # External ID = URL slug (last path component)
    slug = url.rstrip("/").split("/")[-1]

    return {
        "site_id": "em-dk-aktuelt",
        "external_id": slug,
        "post_number": slug,
        "title": title,
        "abstract": abstract,
        "url": url,
        "pdf_url": pdf_url,
        "published_date": pub_date or None,
        "listed_date": pub_date or None,   # no separate listed date on this site
        "category": category or None,
        "publisher": "Erhvervsministeriet",
        "authors": None,
        "keywords": None,
        "department": None,
        "journal": None,
        "doi": None,
        "original_filename": original_filename,
        "metadata": json.dumps(
            {
                "posted_date": pub_date or None,
                "originalFilename": original_filename,
                "category": category or None,
                "slug": slug,
            },
            ensure_ascii=False,
        ),
    }


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class EmDkAktueltCrawler(BaseCrawler):
    site_id = "em-dk-aktuelt"
    site_name = "Custom: em-dk-aktuelt"
    base_url = "https://www.em.dk"

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _discover_month_paths(self) -> list[str]:
        """Return unique /aktuelt/udgivelser-og-aftaler/YEAR/MONTH paths.

        Strategy:
        1. Fetch the index page to discover year-level paths.
        2. Fetch each year page; its left-nav lists all month sub-pages.
        3. The first year's page typically has the full nav for all years,
           so we try to extract all months from there before falling back
           to fetching each year page individually.
        """
        # Step 1 — get year links from the index page
        html = _curl_get(_INDEX_URL)
        if not html:
            print("[em-dk-aktuelt] failed to fetch index page")
            return []

        year_paths: list[str] = []
        year_seen: set = set()
        for m in re.finditer(
            r'href="(/aktuelt/udgivelser-og-aftaler/\d{4})"', html
        ):
            p = m.group(1)
            if p not in year_seen:
                year_seen.add(p)
                year_paths.append(p)

        if not year_paths:
            print("[em-dk-aktuelt] no year paths found on index page")
            return []

        print(f"[em-dk-aktuelt] found {len(year_paths)} year pages to scan")

        # Step 2 — fetch the first year page; its left-nav often contains
        # all months for every year (collapsed but present in HTML).
        first_year_html = _curl_get(_BASE_URL + year_paths[0])
        month_paths: list[str] = []
        month_seen: set = set()

        def _extract_months(src_html: str) -> int:
            added = 0
            for m in re.finditer(
                r'href="(/aktuelt/udgivelser-og-aftaler/\d{4}/[a-z]+)"', src_html
            ):
                p = m.group(1)
                if p not in month_seen:
                    month_seen.add(p)
                    month_paths.append(p)
                    added += 1
            return added

        if first_year_html:
            found = _extract_months(first_year_html)
            print(f"[em-dk-aktuelt] extracted {found} month paths from first year page")

        # If the first year page's nav only shows that year's months, we need
        # to visit each remaining year page individually.
        covered_years = {
            re.search(r"/(\d{4})/", p).group(1)
            for p in month_paths
            if re.search(r"/(\d{4})/", p)
        }
        for year_path in year_paths:
            yr = year_path.rstrip("/").split("/")[-1]
            if yr in covered_years:
                continue
            time.sleep(0.5)
            yr_html = _curl_get(_BASE_URL + year_path)
            if yr_html:
                found = _extract_months(yr_html)
                print(f"[em-dk-aktuelt] year {yr}: {found} month paths")

        return month_paths

    # ------------------------------------------------------------------
    # Per-month item listing via proxy.gba
    # ------------------------------------------------------------------

    def _fetch_month_item_urls(
        self,
        month_path: str,
        max_items: int | None = None,
    ) -> list[str]:
        """Return unique item detail-page URLs for all items in one month page.

        Paginates through the GoBasic proxy.gba API until lastPage=true or max_items reached.
        """
        html = _curl_get(_BASE_URL + month_path)
        if not html:
            print(f"[em-dk-aktuelt] failed to fetch month page: {month_path}")
            return []

        config = _extract_itemlist_config(html)
        if not config:
            print(f"[em-dk-aktuelt] no itemlist config in month page: {month_path}")
            return []

        results: list[str] = []
        seen_urls: set = set()

        for page_num in range(1, _MAX_PAGES + 1):
            if page_num == _MAX_PAGES:
                print(
                    f"[em-dk-aktuelt] safety cap of {_MAX_PAGES} pages reached "
                    f"for month {month_path}"
                )

            payload = {
                "control": _GENERATOR,
                "method": "GetPage",
                "path": month_path,
                "query": "",
                "args": {
                    "arg0": config,
                    "arg1": page_num,
                    "arg2": {"query": "", "categorizations": []},
                    "arg3": "",
                },
            }

            resp = _curl_post_proxy(month_path, payload)
            if resp is None:
                print(
                    f"[em-dk-aktuelt] proxy.gba failed for {month_path} page {page_num}"
                )
                break

            if not resp.get("success"):
                print(
                    f"[em-dk-aktuelt] proxy.gba non-success for {month_path}: {resp}"
                )
                break

            value = resp.get("value", {})
            if isinstance(value, dict) and not value.get("success", True):
                print(
                    f"[em-dk-aktuelt] proxy.gba inner error for {month_path}: "
                    f"{value.get('errorMessage', '')}"
                )
                break

            page_html = value.get("page", "") if isinstance(value, dict) else ""
            last_page = value.get("lastPage", True) if isinstance(value, dict) else True

            if not page_html:
                break

            urls = _extract_item_urls(page_html)
            if not urls:
                break

            new_urls = [u for u in urls if u not in seen_urls]
            seen_urls.update(urls)

            if not new_urls:
                # Paginator silently looped back to page 1 — stop
                break

            for u in new_urls:
                if max_items is not None and len(results) >= max_items:
                    break
                results.append(u)

            if last_page:
                break
            if max_items is not None and len(results) >= max_items:
                break

        return results

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        limit_display = limit if limit is not None else "inf"
        deadline = time.time() + _WALL_MINUTES * 60

        month_paths = self._discover_month_paths()
        if not month_paths:
            print("[em-dk-aktuelt] no month pages discovered, aborting.")
            return 0

        print(
            f"[em-dk-aktuelt] discovered {len(month_paths)} month pages; "
            f"limit={limit_display}"
        )

        for month_idx, month_path in enumerate(month_paths):
            if time.time() > deadline:
                print(
                    f"[em-dk-aktuelt] {_WALL_MINUTES}-minute wall-clock budget "
                    f"reached at month {month_idx+1}/{len(month_paths)}, stopping."
                )
                break
            if limit is not None and saved >= limit:
                break

            if month_idx == 0 or month_idx % 10 == 0:
                print(
                    f"[em-dk-aktuelt] month {month_idx+1}/{len(month_paths)}: "
                    f"{month_path}, saved {saved}/{limit_display}"
                )

            remaining = (limit - saved) if limit is not None else None
            item_urls = self._fetch_month_item_urls(month_path, max_items=remaining)

            if item_urls:
                time.sleep(0.5)

            for item_url in item_urls:
                if limit is not None and saved >= limit:
                    break
                if time.time() > deadline:
                    break
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)

                try:
                    time.sleep(_DETAIL_DELAY)
                    html = _curl_get(item_url)
                    if not html:
                        print(
                            f"[em-dk-aktuelt] item fetch failed, skipping: {item_url}"
                        )
                        continue

                    detail = _parse_detail(item_url, html)
                    if not detail:
                        print(
                            f"[em-dk-aktuelt] item parse failed, skipping: {item_url}"
                        )
                        continue

                    abstract = detail.get("abstract", "")
                    if len(abstract) < _MIN_ABSTRACT:
                        print(
                            f"[em-dk-aktuelt] abstract too short "
                            f"({len(abstract)} chars), skipping: {item_url}"
                        )
                        continue

                    self._save_paper(detail)
                    saved += 1
                    print(
                        f"[em-dk-aktuelt] saved [{saved}] "
                        f"{detail.get('title', '')[:70]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[em-dk-aktuelt] item {item_url} failed: {exc}")
                    continue

        print(f"[em-dk-aktuelt] crawl complete: {saved} records saved.")
        return saved
