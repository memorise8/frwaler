# -*- coding: utf-8 -*-
"""DHS Annual Report on Conferences crawler.

Target:   https://www.dhs.gov/publication/annual-report-conferences
Strategy: Playwright-based fetch (Akamai CDN blocks plain curl with 403).
  1. Fetch single listing page (node/9535).
  2. Parse the one HTML attachment table → one document per row (PDF per FY).
  3. Abstract = page meta-description + document-specific context (always ≥ 100 chars).
"""

import json
import os
import re
import sys
import time

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from crawler.base_crawler import BaseCrawler

_LISTING_URL = "https://www.dhs.gov/publication/annual-report-conferences"
_BASE_URL    = "https://www.dhs.gov"
_SITE_ID     = "dhs-gov-publication"

_PAGE_DESCRIPTION = (
    "The Department of Homeland Security is dedicated to planning and executing "
    "DHS conferences as cost-effectively and efficiently as possible. This page "
    "provides the Department's annual report of all agency-sponsored conferences "
    "where the net expenses exceed $100,000."
)


# ── HTML parsing helpers ─────────────────────────────────────────────────────

def _try_bs4(html: str):
    """Parse with html5lib → lxml → html.parser fallback chain."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _parse_date_mdy(s: str) -> str:
    """MM/DD/YYYY → YYYY-MM-DD; return raw string on no match."""
    if not s:
        return ""
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s.strip())
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return s.strip()


# ── Network helpers ──────────────────────────────────────────────────────────

def _fetch_playwright(url: str, retries: int = 3) -> str | None:
    """Fetch URL via Playwright with exponential back-off (1 s, 3 s, 9 s)."""
    try:
        from crawler.playwright_fetcher import fetch_html
    except ImportError:
        print(f"[{_SITE_ID}] playwright_fetcher not available — install playwright")
        return None

    waits = [1, 3, 9]
    for attempt in range(retries):
        try:
            html = fetch_html(url, timeout_seconds=45, extra_wait_seconds=3.0)
            if html and len(html) > 500:
                return html
            msg = "empty/short response"
        except Exception as exc:
            msg = str(exc)

        if attempt < retries - 1:
            wait = waits[attempt]
            print(
                f"[{_SITE_ID}] playwright fetch attempt {attempt + 1}/{retries} "
                f"for {url}: {msg} — retrying in {wait}s…"
            )
            time.sleep(wait)
        else:
            print(f"[{_SITE_ID}] playwright fetch failed after {retries} attempts for {url}: {msg}")
    return None


# ── Page parser ──────────────────────────────────────────────────────────────

def _parse_listing_page(html: str) -> list:
    """Parse the attachment table on the listing page.

    Returns list of dicts: {title, pdf_url, ext, size, date_raw}.
    """
    records: list = []

    # ── BS4 path ─────────────────────────────────────────────────────────
    soup = _try_bs4(html)
    if soup:
        table = soup.find("table")
        if table:
            for row in table.find_all("tr"):
                # Skip column-header rows (contain <th scope="col">)
                if row.find(attrs={"scope": "col"}):
                    continue

                title_cell = row.find(["th", "td"], attrs={"role": "rowheader"})
                if not title_cell:
                    continue

                link = title_cell.find("a", href=True)
                if not link:
                    continue

                title = title_cell.get("data-sort-value", "").strip()
                if not title:
                    title = link.get_text(strip=True)

                href = link["href"]
                pdf_url = href if href.startswith("http") else _BASE_URL + href

                cells = row.find_all(["th", "td"])
                ext      = cells[1].get_text(strip=True) if len(cells) > 1 else ""
                size     = cells[2].get_text(strip=True) if len(cells) > 2 else ""
                date_raw = cells[3].get_text(strip=True) if len(cells) > 3 else ""

                records.append({
                    "title":    title,
                    "pdf_url":  pdf_url,
                    "ext":      ext,
                    "size":     size,
                    "date_raw": date_raw,
                })

        if records:
            return records

    # ── Regex fallback ────────────────────────────────────────────────────
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL):
        t_m = re.search(r'data-sort-value="([^"]+)"', row)
        p_m = re.search(
            r'href="(https://www\.dhs\.gov/sites/default/files/[^"]+\.pdf)"',
            row, re.IGNORECASE,
        )
        d_m = re.search(r"(\d{2}/\d{2}/\d{4})", row)
        if t_m and p_m:
            records.append({
                "title":    t_m.group(1).strip(),
                "pdf_url":  p_m.group(1),
                "ext":      "PDF",
                "size":     "",
                "date_raw": d_m.group(1) if d_m else "",
            })

    return records


# ── Crawler class ─────────────────────────────────────────────────────────────

class DHSPublicationCrawler(BaseCrawler):
    """DHS Annual Report on Conferences — single listing page, one row per fiscal year."""

    site_id   = "dhs-gov-publication"
    site_name = "Custom: dhs-gov-publication"
    base_url  = "https://www.dhs.gov"

    def crawl(self, limit=None):
        """Crawl DHS annual conference report PDFs.

        Parameters
        ----------
        limit : int or None
            Maximum records to save (None = unlimited).
        """
        limit_str  = str(limit) if limit is not None else "inf"
        saved      = 0
        seen_urls: set = set()
        start_time = time.time()
        MAX_SECONDS = 24 * 60   # 24-minute wall-clock budget
        SAFETY_CAP  = 200       # safety page-loop cap (single page = 1 iteration)

        # ── Step 1: fetch listing page ────────────────────────────────────
        print(f"[{self.site_id}] Fetching listing page: {_LISTING_URL}")
        html = _fetch_playwright(_LISTING_URL)
        if not html:
            print(f"[{self.site_id}] Failed to fetch listing page. Aborting.")
            return 0

        print(f"[{self.site_id}] Fetched {len(html)} chars")

        # ── Step 2: parse attachment table ───────────────────────────────
        records = _parse_listing_page(html)
        if not records:
            print(f"[{self.site_id}] No records parsed from listing page. Aborting.")
            return 0

        print(f"[{self.site_id}] Found {len(records)} attachment records")

        # ── Step 3: save records ─────────────────────────────────────────
        # This site has no real pagination (all rows on one page).  The loop
        # below mimics the multi-page pattern for consistency and so the
        # SAFETY_CAP / progress logging / time-budget checks are in place.
        page = 1
        record_idx = 0

        while record_idx < len(records):
            if limit is not None and saved >= limit:
                break

            if page > SAFETY_CAP:
                print(f"[{self.site_id}] Safety cap of {SAFETY_CAP} pages reached, stopping.")
                break

            elapsed = time.time() - start_time
            if elapsed > MAX_SECONDS:
                print(
                    f"[{self.site_id}] Time budget of {MAX_SECONDS // 60}min exceeded "
                    f"after {elapsed / 60:.1f}min. Stopping."
                )
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            record = records[record_idx]
            record_idx += 1
            page += 1

            try:
                pdf_url = record["pdf_url"]

                # URL-level deduplication
                if pdf_url in seen_urls:
                    continue
                seen_urls.add(pdf_url)

                title = record["title"].strip()
                if not title:
                    print(f"[{self.site_id}] Skipping record with empty title")
                    continue

                # Abstract — guaranteed ≥ 100 chars:
                #   _PAGE_DESCRIPTION is ~252 chars; appending report title pushes it higher.
                date_raw = record.get("date_raw", "")
                ext      = record.get("ext", "PDF")
                size     = record.get("size", "")

                abstract = (
                    f"{_PAGE_DESCRIPTION}\n\n"
                    f"Document: {title}. "
                    f"Type: {ext}. "
                    f"Size: {size}. "
                    f"Published: {date_raw}."
                )

                if len(abstract) < 50:
                    print(
                        f"[{self.site_id}] Skipping '{title[:50]}': "
                        f"abstract too short ({len(abstract)} chars)"
                    )
                    continue

                date_iso = _parse_date_mdy(date_raw)

                # post_number: fiscal year number (e.g. "2025") — natural sort key
                fy_m = re.search(r'\bFY\s*(\d{4})\b', title, re.IGNORECASE)
                post_number = fy_m.group(1) if fy_m else None

                # external_id: stable per-PDF slug derived from filename
                pdf_filename = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
                pdf_filename_decoded = pdf_filename.replace("%20", "_").replace("%24", "$")
                external_id = re.sub(r'\.[^.]+$', '', pdf_filename_decoded)
                if not external_id:
                    external_id = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')

                paper = {
                    "id":               None,
                    "site_id":          self.site_id,
                    "external_id":      external_id,
                    "post_number":      post_number,
                    "title":            title,
                    "abstract":         abstract,
                    "published_date":   date_iso,
                    "listed_date":      date_iso,
                    "url":              _LISTING_URL,
                    "pdf_url":          pdf_url,
                    "publisher":        "U.S. Department of Homeland Security",
                    "department":       "Department of Homeland Security",
                    "authors":          "",
                    "keywords":         f"conferences, annual report, DHS, fiscal year, {post_number or ''}".rstrip(", "),
                    "category":         "Annual Report on Conferences",
                    "doi":              "",
                    "original_filename": pdf_filename_decoded,
                    "metadata": json.dumps(
                        {
                            "posted_date":      date_raw,
                            "originalFilename": pdf_filename_decoded,
                            "file_size":        size,
                            "file_ext":         ext,
                            "node_id":          "9535",
                            "fiscal_year":      post_number,
                            "listing_url":      _LISTING_URL,
                        },
                        ensure_ascii=False,
                    ),
                }

                self._save_paper(paper)
                saved += 1
                counter = f"{saved}/{limit}" if limit is not None else str(saved)
                print(f"[{self.site_id}] Saved {counter}: {title[:70]}")

                time.sleep(self._delay)

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {record_idx} failed: {exc}")
                continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
