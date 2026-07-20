# -*- coding: utf-8 -*-
"""Crawler for MBIE energy statistics and publications pages.

Starting URL:
  https://www.mbie.govt.nz/building-and-energy/energy-and-natural-resources/
  energy-statistics-and-modelling/energy-statistics/energy-balances

Strategy: discover all direct child pages under the two energy sub-sections
(energy-statistics/ and energy-publications-and-technical-papers/), then
scrape each page for title / abstract / download links.  Each CMS page
becomes one document record.
"""

import json
import re
import subprocess
import sys
import time
from urllib.parse import urlparse

# Absolute import required — spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_SITE_ID = "mbie-govt-nz-building-and-energy"
_BASE = "https://www.mbie.govt.nz"

_START_URL = (
    _BASE
    + "/building-and-energy/energy-and-natural-resources/"
    "energy-statistics-and-modelling/energy-statistics/energy-balances"
)

# Parent section paths whose direct children are the documents to crawl
_SECTION_PATHS = [
    "/building-and-energy/energy-and-natural-resources/"
    "energy-statistics-and-modelling/energy-statistics",
    "/building-and-energy/energy-and-natural-resources/"
    "energy-statistics-and-modelling/energy-publications-and-technical-papers",
]

_PUBLISHER = "Ministry of Business, Innovation & Employment"
_BUDGET_SECS = 25 * 60  # 25-minute wall-clock budget

_MONTHS = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
}


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _curl_get(url: str, retries: int = 3) -> str | None:
    """Fetch URL via curl with TLS-max-1.3, exponential backoff."""
    cmd = [
        "curl", "--tls-max", "1.3", "-skL", "--max-time", "30",
        "-A",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: en-NZ,en;q=0.9",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            if result.returncode == 0 and result.stdout:
                try:
                    return result.stdout.decode("utf-8")
                except UnicodeDecodeError:
                    return result.stdout.decode("utf-8", errors="replace")
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[{_SITE_ID}] empty response for {url}, retry in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[{_SITE_ID}] curl error: {exc}, retry in {wait}s")
                time.sleep(wait)
            else:
                print(
                    f"[{_SITE_ID}] curl failed after {retries} attempts "
                    f"for {url}: {exc}"
                )
    return None


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

def _bs(raw: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    if not raw:
        return None
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _parse_date(text: str) -> str | None:
    """Extract first 'DD Month YYYY' date from text → ISO YYYY-MM-DD."""
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text)
    if m:
        day, mon_str, year = m.group(1), m.group(2).lower(), m.group(3)
        mon = _MONTHS.get(mon_str)
        if mon:
            return f"{year}-{mon}-{int(day):02d}"
    # Month YYYY fallback
    m = re.search(r"\b([A-Za-z]+)\s+(20\d{2})\b", text)
    if m:
        mon_str, year = m.group(1).lower(), m.group(2)
        mon = _MONTHS.get(mon_str)
        if mon:
            return f"{year}-{mon}-01"
    return None


def _clean_entities(text: str) -> str:
    return (
        text.replace("&amp;", "&")
            .replace("&rsaquo;", "›")
            .replace("&nbsp;", " ")
            .replace("&#39;", "'")
            .replace("&quot;", '"')
    )


# ---------------------------------------------------------------------------
# Page content extraction
# ---------------------------------------------------------------------------

def _extract_page(html: str, url: str) -> dict | None:
    """Extract structured data from an MBIE CMS content page."""
    soup = _bs(html)
    if soup is None:
        return None

    # --- title ---
    title = ""
    title_tag = soup.find("title")
    if title_tag:
        title = title_tag.get_text(strip=True)
        title = re.sub(
            r"\s*\|\s*Ministry of Business.*$", "", title, flags=re.I
        ).strip()
        title = re.sub(r"\s*\|\s*MBIE$", "", title, flags=re.I).strip()

    # --- meta description ---
    meta_desc = ""
    meta = soup.find("meta", attrs={"name": "description"})
    if meta:
        meta_desc = (meta.get("content") or "").strip()

    # --- abstract: meta desc + page-intro + body paragraphs ---
    abstract_parts: list[str] = []
    if meta_desc:
        abstract_parts.append(meta_desc)

    # page-intro class often contains the canonical intro paragraph
    intro_tag = soup.find(class_="page-intro")
    if intro_tag:
        intro_text = intro_tag.get_text(separator=" ", strip=True)
        if intro_text and intro_text not in abstract_parts:
            abstract_parts.append(intro_text)

    # Walk <p> tags inside the main content area for richer text
    main_tag = (
        soup.find("main")
        or soup.find("div", attrs={"id": "main"})
        or soup.find("div", attrs={"id": "content"})
        or soup.body
    )
    if main_tag:
        for p in main_tag.find_all("p"):
            txt = p.get_text(separator=" ", strip=True)
            if len(txt) >= 40 and txt not in abstract_parts:
                abstract_parts.append(txt)
            if len(" ".join(abstract_parts)) >= 500:
                break

    abstract = _clean_entities(
        re.sub(r"\s+", " ", " ".join(abstract_parts)).strip()
    )

    # --- date: first recognisable date in page text ---
    full_text = soup.get_text(separator=" ")
    pub_date = _parse_date(full_text)

    # --- downloadable file links ---
    files: list[str] = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if re.search(r"\.(xlsx|pdf|csv|xls|zip|ods)(\?.*)?$", href, re.I):
            if href.startswith("/"):
                href = _BASE + href
            if href not in files:
                files.append(href)

    pdf_url = files[0] if files else None
    original_filename = None
    if pdf_url:
        original_filename = pdf_url.split("/")[-1].split("?")[0]

    slug = urlparse(url).path.rstrip("/").split("/")[-1]

    return {
        "external_id": slug,
        "post_number": slug,
        "title": title,
        "abstract": abstract,
        "published_date": pub_date,
        "listed_date": pub_date,
        "url": url,
        "pdf_url": pdf_url,
        "original_filename": original_filename,
        "metadata": json.dumps(
            {"slug": slug, "all_files": files, "page_url": url},
            ensure_ascii=False,
        ),
    }


# ---------------------------------------------------------------------------
# Section discovery
# ---------------------------------------------------------------------------

def _discover_sub_pages(section_path: str) -> list[str]:
    """Return direct-child page URLs one level below *section_path*."""
    url = _BASE + section_path
    html = _curl_get(url)
    if not html:
        print(f"[{_SITE_ID}] Could not fetch section index: {url}")
        return []

    soup = _bs(html)
    if soup is None:
        return []

    found: list[str] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if href.startswith("/"):
            full = _BASE + href
        elif href.startswith(_BASE):
            full = href
        else:
            continue

        parsed = urlparse(full)
        path = parsed.path.rstrip("/")
        if not path.startswith(section_path + "/"):
            continue
        # Exactly one level deeper
        rest = path[len(section_path) + 1:]
        if "/" in rest or not rest:
            continue
        # No fragment / query
        if parsed.fragment or parsed.query:
            continue
        if full not in seen:
            seen.add(full)
            found.append(full)

    return found


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class MBIEBuildingAndEnergyCrawler(BaseCrawler):
    """Crawls MBIE energy statistics and publications CMS pages."""

    site_id = _SITE_ID
    site_name = "Custom: mbie-govt-nz-building-and-energy"
    base_url = _BASE

    def crawl(self, limit=None) -> int:
        start_ts = time.time()
        saved = 0
        seen_urls: set[str] = set()

        # ------------------------------------------------------------------
        # 1. Build the list of pages to visit
        # ------------------------------------------------------------------
        candidate_urls: list[str] = [_START_URL]

        for sec_path in _SECTION_PATHS:
            discovered = _discover_sub_pages(sec_path)
            print(
                f"[{_SITE_ID}] Discovered {len(discovered)} pages "
                f"from {_BASE + sec_path}"
            )
            candidate_urls.extend(discovered)

        # Deduplicate while preserving order
        unique_urls: list[str] = []
        dedup: set[str] = set()
        for u in candidate_urls:
            key = u.rstrip("/")
            if key not in dedup:
                dedup.add(key)
                unique_urls.append(u)

        limit_str = str(limit) if limit is not None else "∞"
        print(
            f"[{_SITE_ID}] Total candidate pages: {len(unique_urls)} "
            f"| limit={limit_str}"
        )

        # ------------------------------------------------------------------
        # 2. Crawl each page
        # ------------------------------------------------------------------
        page_num = 0

        for page_url in unique_urls:
            if limit is not None and saved >= limit:
                break

            if time.time() - start_ts > _BUDGET_SECS:
                print(
                    f"[{_SITE_ID}] Wall-clock budget reached "
                    f"({time.time() - start_ts:.0f}s), stopping."
                )
                break

            if page_url in seen_urls:
                continue
            seen_urls.add(page_url)

            page_num += 1
            if page_num % 10 == 0:
                print(
                    f"[{_SITE_ID}] page {page_num}: "
                    f"saved {saved}/{limit_str}"
                )

            try:
                html = _curl_get(page_url)
                if not html:
                    print(f"[{_SITE_ID}] Failed to fetch {page_url}, skipping.")
                    continue

                time.sleep(self._delay)

                info = _extract_page(html, page_url)
                if info is None:
                    print(f"[{_SITE_ID}] Failed to parse {page_url}, skipping.")
                    continue

                if not info.get("title"):
                    print(f"[{_SITE_ID}] No title at {page_url}, skipping.")
                    continue

                abstract = info.get("abstract", "")
                if len(abstract) < 50:
                    print(
                        f"[{_SITE_ID}] Abstract too short "
                        f"({len(abstract)} chars) at {page_url}, skipping."
                    )
                    continue

                self._save_paper({
                    "site_id": self.site_id,
                    "external_id": info["external_id"],
                    "post_number": info["post_number"],
                    "title": info["title"],
                    "abstract": abstract,
                    "published_date": info.get("published_date"),
                    "posted_date": info.get("listed_date"),
                    "url": info["url"],
                    "pdf_url": info.get("pdf_url"),
                    "original_filename": info.get("original_filename"),
                    "publisher": _PUBLISHER,
                    "authors": "",
                    "keywords": (
                        "energy statistics,New Zealand,MBIE,"
                        "energy balances,energy data"
                    ),
                    "category": "Energy Statistics",
                    "metadata": info.get("metadata"),
                })
                saved += 1
                print(
                    f"[{_SITE_ID}] Saved [{saved}/{limit_str}]: "
                    f"{info['title'][:55]} "
                    f"(abstract {len(abstract)} chars)"
                )

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] item {page_url} failed: {exc}")
                continue

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
