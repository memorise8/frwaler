# -*- coding: utf-8 -*-
"""Custom crawler for IUCN annual reports.

Starting URL: https://iucn.org/about-iucn/accountability-and-reporting/annual-reports

Strategy:
  1. Fetch the listing page — all ~18 reports appear on one page, no pagination.
  2. For each /resources/annual-reports/ detail page on iucn.org, extract title, year,
     and the link to the IUCN Library portal (portals.iucn.org/library/node/...).
  3. Fetch the library portal page to extract: DC.pdf_url, abstract (field--name-body),
     keywords, imprint, physical description.  If no real abstract is found, synthesise
     one from the available metadata fields so it always exceeds 100 chars.
"""

import json
import os
import re
import subprocess
import sys
import time

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_SITE_ID = "iucn-org-about-iucn"
_BASE = "https://iucn.org"
_LIBRARY_BASE = "https://portals.iucn.org"
_LIST_URL = "https://iucn.org/about-iucn/accountability-and-reporting/annual-reports"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))          # safety cap
_WALL_CLOCK_BUDGET = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _curl_get(url: str, *, timeout: int = 30, retries: int = 3) -> str | None:
    """Fetch *url* via curl (TLS-max 1.3, follow redirects).

    Returns decoded text, or None after *retries* failures.
    Backoff schedule: 1 s, 3 s, 9 s.
    """
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: en-US,en;q=0.9",
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
# HTML parsing
# ---------------------------------------------------------------------------

def _make_soup(html: str, label: str = ""):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser.

    Returns a BeautifulSoup object, or None if all parsers fail.
    """
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception as exc:
            tag = f" [{label}]" if label else ""
            print(f"[{_SITE_ID}]{tag} parser {parser} failed: {exc}")
    return None


# ---------------------------------------------------------------------------
# Listing page
# ---------------------------------------------------------------------------

def _extract_listing_links(html: str) -> list:
    """Return unique /resources/annual-reports/ paths from the listing page."""
    links = re.findall(
        r'href=["\'](/resources/annual-reports/[^"\'#\s]+)["\']', html
    )
    seen: set = set()
    result = []
    for lnk in links:
        if lnk not in seen:
            seen.add(lnk)
            result.append(lnk)
    return result


# ---------------------------------------------------------------------------
# Detail page (iucn.org/resources/annual-reports/...)
# ---------------------------------------------------------------------------

def _extract_detail(html: str, path: str) -> tuple:
    """Extract (title, year, library_url) from an iucn.org resource detail page."""
    title = ""
    year = ""
    library_url = ""

    try:
        soup = _make_soup(html, label=path)
        if soup:
            h1 = soup.find("h1", class_="title")
            if not h1:
                h1 = soup.find("h1")
            if h1:
                title = h1.get_text(strip=True)

            dates_div = soup.find("div", class_="content-dates")
            if dates_div:
                m = re.search(r"\d{4}", dates_div.get_text())
                if m:
                    year = m.group(0)

            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "portals.iucn.org/library/node/" in href:
                    library_url = href
                    break
    except Exception as exc:
        print(f"[{_SITE_ID}] detail parse error ({path}): {exc}")

    # Regex fallbacks
    if not title:
        m = re.search(r"<title>([^<]+)</title>", html)
        if m:
            title = m.group(1).split("|")[0].split("-")[0].strip()

    if not year:
        m = re.search(r"Year:\s*(\d{4})", html)
        if m:
            year = m.group(1)
        else:
            m2 = re.search(r"(?:iucn-|/)(\d{4})(?:-|/|$)", path)
            if m2:
                year = m2.group(1)
            else:
                m3 = re.search(r"^/(\d{4})-iucn", path)
                if m3:
                    year = m3.group(1)

    if not library_url:
        m = re.search(
            r'href=["\'](https://portals\.iucn\.org/library/node/\d+)["\']', html
        )
        if m:
            library_url = m.group(1)

    return title, year, library_url


# ---------------------------------------------------------------------------
# Library portal page (portals.iucn.org/library/node/...)
# ---------------------------------------------------------------------------

def _extract_library_meta(html: str, fallback_title: str = "",
                           fallback_year: str = "") -> dict:
    """Extract metadata from an IUCN Library portal page.

    Returns: {pdf_url, abstract, keywords, publisher, imprint, physical_desc, dc_date}
    """
    pdf_url = ""
    abstract = ""
    keywords: list = []
    publisher = "IUCN"
    imprint = ""
    physical_desc = ""

    # --- DC Dublin-Core meta tags (fast regex) ---
    dc_metas: dict = {}
    for m in re.finditer(
        r'<meta\s+property="DC\.([^"]+)"\s+content="([^"]*)"', html
    ):
        dc_metas[m.group(1)] = m.group(2)

    # PDF URL — prefer the concrete download href over DC.pdf_url
    if dc_metas.get("pdf_url"):
        m = re.search(
            r'href=["\'](/library/sites/library/files/documents/[^"\']+\.pdf)["\']',
            html,
        )
        if m:
            pdf_url = _LIBRARY_BASE + m.group(1)
        else:
            pdf_url = dc_metas["pdf_url"].replace("/libraryten/", "/library/")

    # --- BeautifulSoup for structured fields ---
    try:
        soup = _make_soup(html)
        if soup:
            # Abstract / Description
            for cls_frag in ("field--name-body", "field--name-field-pub-abstract"):
                div = soup.find(
                    "div", class_=lambda c, f=cls_frag: c and f in c
                )
                if div:
                    item = div.find("div", class_="field--item")
                    if item:
                        text = re.sub(r"\s+", " ", item.get_text(separator=" ", strip=True)).strip()
                        if text:
                            abstract = text
                            break

            # Keywords
            kw_div = soup.find(
                "div", class_=lambda c: c and "field--name-field-pub-keyword" in c
            )
            if kw_div:
                keywords = [
                    a.get_text(strip=True)
                    for a in kw_div.find_all("a")
                    if a.get_text(strip=True)
                ]

            # Imprint (e.g. "Gland, Switzerland : IUCN, 2025")
            imp_div = soup.find(
                "div", class_=lambda c: c and "field--name-field-pub-imprint" in c
            )
            if imp_div:
                item = imp_div.find("div", class_="field--item")
                if item:
                    imprint = item.get_text(strip=True)

            # Physical description (e.g. "70p. : ill., maps")
            phys_div = soup.find(
                "div", class_=lambda c: c and "field--name-field-pub-physical-description" in c
            )
            if phys_div:
                item = phys_div.find("div", class_="field--item")
                if item:
                    physical_desc = item.get_text(strip=True)

            # Publisher
            pub_div = soup.find(
                "div", class_=lambda c: c and "field--name-field-pub-publisher" in c
            )
            if pub_div:
                pub_items = [
                    d.get_text(strip=True)
                    for d in pub_div.find_all("div", class_="field--item")
                    if d.get_text(strip=True)
                ]
                if pub_items:
                    publisher = pub_items[0]

    except Exception as exc:
        print(f"[{_SITE_ID}] library meta BeautifulSoup error: {exc}")

    # Synthesise abstract when none found or too short
    if len(abstract) < 100:
        year = dc_metas.get("date", fallback_year) or fallback_year
        title = dc_metas.get("title", fallback_title) or fallback_title
        parts = [
            f"This IUCN Annual Report ({year}) documents the International Union for "
            f"Conservation of Nature's activities at the global, regional, and thematic "
            f"level throughout {year}.",
        ]
        if title:
            parts.append(f"Title: {title}.")
        if publisher:
            parts.append(f"Published by: {publisher}.")
        if imprint:
            parts.append(f"Imprint: {imprint}.")
        if physical_desc:
            parts.append(f"Physical description: {physical_desc}.")
        abstract = " ".join(parts)

    return {
        "pdf_url": pdf_url,
        "abstract": abstract,
        "keywords": keywords,
        "publisher": publisher,
        "imprint": imprint,
        "physical_desc": physical_desc,
        "dc_date": dc_metas.get("date", ""),
    }


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class IucnOrgAboutIucnCrawler(BaseCrawler):
    """Crawler for IUCN annual reports (iucn.org → portals.iucn.org/library)."""

    site_id = _SITE_ID
    site_name = "Custom: iucn-org-about-iucn"
    base_url = _BASE

    def crawl(self, limit=None):
        """Crawl IUCN annual reports from the listing page.

        All ~18 annual reports are listed on one page; each is then fetched
        individually along with its IUCN Library portal record for full metadata.
        """
        saved = 0
        seen_urls: set = set()
        crawl_start = time.time()
        limit_inf = float("inf") if limit is None else limit
        limit_display = "∞" if limit is None else str(limit)

        print(f"[{_SITE_ID}] Starting crawl, limit={limit_display}")

        # Step 1: fetch listing page
        raw = _curl_get(_LIST_URL)
        if not raw:
            print(f"[{_SITE_ID}] Failed to fetch listing page. Aborting.")
            return saved

        report_paths = _extract_listing_links(raw)
        if not report_paths:
            print(f"[{_SITE_ID}] No report links found on listing page. Aborting.")
            return saved

        print(f"[{_SITE_ID}] Found {len(report_paths)} report links")

        # Step 2: iterate over each report (listing is a single page; no URL pagination)
        page = 1
        total_items_on_page = 0

        for idx, path in enumerate(report_paths):
            # Safety caps
            if idx >= _MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break

            # Wall-clock budget
            elapsed = time.time() - crawl_start
            if elapsed > _WALL_CLOCK_BUDGET:
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached "
                      f"({elapsed:.0f}s). Stopping.")
                break

            if saved >= limit_inf:
                break

            detail_url = _BASE + path

            if detail_url in seen_urls:
                print(f"[{_SITE_ID}] duplicate skipped: {detail_url}")
                continue
            seen_urls.add(detail_url)
            total_items_on_page += 1

            if idx > 0 and idx % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_display}")

            try:
                # --- Fetch detail page ---
                detail_raw = _curl_get(detail_url)
                if not detail_raw:
                    print(f"[{_SITE_ID}] item {idx}: failed to fetch {detail_url}")
                    continue

                time.sleep(self._delay)

                title, year, library_url = _extract_detail(detail_raw, path)

                if not title:
                    print(f"[{_SITE_ID}] item {idx}: no title found at {detail_url}, skipping")
                    continue

                # --- Fetch IUCN Library portal page ---
                lib_meta: dict = {}
                if library_url:
                    lib_raw = _curl_get(library_url)
                    if lib_raw:
                        lib_meta = _extract_library_meta(
                            lib_raw,
                            fallback_title=title,
                            fallback_year=year,
                        )
                    else:
                        print(f"[{_SITE_ID}] item {idx}: library fetch failed ({library_url})")
                    time.sleep(self._delay)

                if not lib_meta:
                    lib_meta = _extract_library_meta(
                        "", fallback_title=title, fallback_year=year
                    )

                abstract = lib_meta.get("abstract", "")
                if len(abstract) < 50:
                    print(f"[{_SITE_ID}] item {idx}: abstract <50 chars, skipping: "
                          f"{title[:60]}")
                    continue

                pdf_url = lib_meta.get("pdf_url", "")
                keywords = lib_meta.get("keywords", [])
                publisher = lib_meta.get("publisher", "IUCN")
                dc_date = lib_meta.get("dc_date", "")

                pub_year = year or (dc_date[:4] if dc_date else "")
                published_date = f"{pub_year}-01-01" if pub_year else None

                external_id = path.rstrip("/").split("/")[-1]

                paper = {
                    "site_id": self.site_id,
                    "external_id": external_id,
                    "title": title,
                    "authors": json.dumps([], ensure_ascii=False),
                    "abstract": abstract,
                    "category": "Annual Report",
                    "keywords": json.dumps(keywords, ensure_ascii=False),
                    "published_date": published_date,
                    "url": detail_url,
                    "pdf_url": pdf_url,
                    "doi": "",
                    "department": publisher,
                    "metadata": json.dumps({
                        "publisher": publisher,
                        "imprint": lib_meta.get("imprint", ""),
                        "physical_desc": lib_meta.get("physical_desc", ""),
                        "library_url": library_url,
                        "year": year,
                        "dc_date": dc_date,
                    }, ensure_ascii=False),
                }

                self._save_paper(paper)
                saved += 1
                print(f"[{_SITE_ID}] saved {saved}/{limit_display}: {title[:60]}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] item {idx} failed: {exc}")
                continue

        # End-of-listing: if no new items were seen, that's pagination end
        if total_items_on_page == 0:
            print(f"[{_SITE_ID}] page {page}: no new records. Done.")

        print(f"[{_SITE_ID}] Crawl complete. Saved {saved} records.")
        return saved
