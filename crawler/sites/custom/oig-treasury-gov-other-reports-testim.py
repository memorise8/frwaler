# -*- coding: utf-8 -*-
"""
Crawler for https://oig.treasury.gov/other-reports-testimonies
U.S. Treasury Office of Inspector General — Reports & Testimonies

All records link directly to PDF files; there are no HTML detail pages.
Abstract is constructed from the long descriptive title text.

Sections crawled:
  1. /reports/audit-and-evaluation        (paginated; Agency | Date | Name)
  2. /reports/testimonies-other-documents  (paginated; Name only)
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urlparse, unquote

from crawler.base_crawler import BaseCrawler

_SITE_ID = "oig-treasury-gov-other-reports-testim"
_BASE_URL = "https://oig.treasury.gov"
_PUBLISHER = "U.S. Department of the Treasury Office of Inspector General"
_MAX_PAGES = 200
_ABSTRACT_MIN = 50
_ABSTRACT_SUFFIX = f" | Published by {_PUBLISHER}."

_SECTIONS = [
    {
        "path": "/reports/audit-and-evaluation",
        "category": "Audit and Evaluation Reports",
        "has_agency_col": True,
        "has_date_col": True,
    },
    {
        "path": "/reports/testimonies-other-documents",
        "category": "Testimonies & Other Documents",
        "has_agency_col": False,
        "has_date_col": False,
    },
]

_REPORT_NUM_RE = re.compile(r"^(OIG[-\s][A-Z]{0,3}[-\s]?\d{2}[-\s]\d{3,4}[A-Z]?)\b", re.IGNORECASE)
_DATE_FROM_PATH_RE = re.compile(r"/system/files/(\d{4})-(\d{2})/")


def _make_soup(html_str: str):
    """Parse with html5lib → lxml → html.parser fallback; return soup or None."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html_str, parser)
        except Exception:
            continue
    return None


def _extract_report_num(title: str) -> str | None:
    m = _REPORT_NUM_RE.match(title.strip())
    return m.group(1).strip() if m else None


def _date_from_pdf_path(pdf_url: str) -> str | None:
    m = _DATE_FROM_PATH_RE.search(pdf_url)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return None


def _filename_from_url(url: str) -> str | None:
    try:
        path = urlparse(url).path
        tail = path.rstrip("/").split("/")[-1].split("?")[0]
        return unquote(tail) if tail else None
    except Exception:
        return None


class OigTreasuryOtherReportsTestimCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: oig-treasury-gov-other-reports-testim"
    base_url = _BASE_URL

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl(self, url: str, *, timeout: int = 45) -> str | None:
        """Fetch URL with curl; retry 3x with exponential backoff."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            url,
        ]
        waits = [1, 3, 9]
        last_err = "unknown"
        for attempt, wait in enumerate(waits, start=1):
            try:
                res = subprocess.run(cmd, capture_output=True, timeout=timeout + 10, check=False)
                raw = res.stdout.decode("utf-8", errors="replace")
                if res.returncode == 0 and raw.strip():
                    return raw
                last_err = f"exit={res.returncode} stderr={res.stderr.decode('utf-8', errors='replace').strip()[:120]}"
            except Exception as exc:
                last_err = str(exc)
            if attempt < len(waits):
                print(f"[{_SITE_ID}] curl attempt {attempt}/{len(waits)} failed for {url}: {last_err}; retrying in {wait}s")
                time.sleep(wait)
        print(f"[{_SITE_ID}] curl failed after {len(waits)} attempts for {url}: {last_err}")
        return None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_listing_page(self, html: str, has_agency_col: bool, has_date_col: bool) -> list[dict]:
        """Parse one listing HTML page; return list of raw item dicts."""
        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{_SITE_ID}] BeautifulSoup construction failed: {exc}")
            return []
        if not soup:
            return []

        table = soup.find("table", class_=lambda c: c and "views-view-table" in c)
        if not table:
            return []

        tbody = table.find("tbody")
        if not tbody:
            return []

        items = []
        for row in tbody.find_all("tr"):
            try:
                cells = row.find_all("td")
                if not cells:
                    continue

                agency = None
                date_str = None
                name_cell = None

                if has_agency_col and has_date_col and len(cells) >= 3:
                    agency = cells[0].get_text(strip=True) or None
                    time_tag = cells[1].find("time")
                    if time_tag and time_tag.get("datetime"):
                        # "2026-04-15T00:00:00Z" → "2026-04-15"
                        date_str = time_tag["datetime"][:10]
                    name_cell = cells[2]
                elif has_agency_col and not has_date_col and len(cells) >= 2:
                    agency = cells[0].get_text(strip=True) or None
                    name_cell = cells[1]
                else:
                    name_cell = cells[0]

                if not name_cell:
                    continue

                link = name_cell.find("a")
                if not link:
                    continue

                raw_title = link.get_text(separator=" ", strip=True)
                title = unescape(raw_title).strip()
                pdf_href = link.get("href", "").strip()

                if not pdf_href:
                    continue

                pdf_url = pdf_href if pdf_href.startswith("http") else _BASE_URL + pdf_href

                if not date_str:
                    date_str = _date_from_pdf_path(pdf_url)

                items.append({
                    "title": title,
                    "pdf_url": pdf_url,
                    "date": date_str,
                    "agency": agency,
                })
            except Exception as exc:
                print(f"[{_SITE_ID}] Row parse error: {exc}")
                continue

        return items

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl OIG Treasury reports and save to DB.

        Parameters
        ----------
        limit:
            Max documents to save. None = unlimited.
        """
        limit_val = float("inf") if limit is None else limit
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        MAX_WALL_SECS = 25 * 60

        for section in _SECTIONS:
            if saved >= limit_val:
                break

            sec_path = section["path"]
            category = section["category"]
            has_agency = section["has_agency_col"]
            has_date = section["has_date_col"]
            list_base = _BASE_URL + sec_path

            print(f"[{_SITE_ID}] Starting section: {category}")

            for page_num in range(_MAX_PAGES):
                if saved >= limit_val:
                    break

                elapsed = time.time() - start_time
                if elapsed > MAX_WALL_SECS:
                    print(f"[{_SITE_ID}] 25-minute wall-clock budget reached; stopping.")
                    return saved

                page_url = f"{list_base}?page={page_num}" if page_num > 0 else list_base

                html = self._curl(page_url)
                if not html:
                    print(f"[{_SITE_ID}] Failed to fetch page {page_num} of {sec_path}; aborting section.")
                    break

                try:
                    items = self._parse_listing_page(html, has_agency, has_date)
                except Exception as exc:
                    print(f"[{_SITE_ID}] Page parse error on {page_url}: {exc}")
                    break

                if not items:
                    print(f"[{_SITE_ID}] No items on page {page_num} of {sec_path}; end of section.")
                    break

                new_on_page = 0

                for item in items:
                    if saved >= limit_val:
                        break

                    try:
                        pdf_url = item["pdf_url"]

                        # URL deduplication to prevent infinite loops
                        if pdf_url in seen_urls:
                            continue
                        seen_urls.add(pdf_url)
                        new_on_page += 1

                        title = item["title"]
                        date_str = item.get("date")
                        agency = item.get("agency") or ""

                        # Build abstract: title is the only description available
                        abstract = title
                        if len(abstract) < _ABSTRACT_MIN:
                            print(f"[{_SITE_ID}] Skipping short abstract ({len(abstract)} chars): {title[:60]}")
                            continue
                        if len(abstract) < 100:
                            abstract = abstract + _ABSTRACT_SUFFIX

                        report_num = _extract_report_num(title)
                        external_id = report_num or _filename_from_url(pdf_url) or pdf_url
                        original_filename = _filename_from_url(pdf_url)

                        metadata = {
                            "agency": agency,
                            "category": category,
                            "report_number": report_num,
                            "originalFilename": original_filename,
                            "posted_date": date_str,
                        }

                        paper = {
                            "site_id": _SITE_ID,
                            "external_id": external_id,
                            "title": title,
                            "abstract": abstract,
                            "published_date": date_str,
                            "posted_date": date_str,
                            "url": pdf_url,
                            "pdf_url": pdf_url,
                            "publisher": _PUBLISHER,
                            "category": category,
                            "department": agency or None,
                            "original_filename": original_filename,
                            "metadata": json.dumps(metadata, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[{_SITE_ID}] Item failed ({item.get('title', '')[:60]}): {exc}")
                        continue

                lim_str = str(limit) if limit is not None else "∞"
                if page_num % 10 == 0:
                    print(f"[{_SITE_ID}] page {page_num}: saved {saved}/{lim_str}")

                if page_num == _MAX_PAGES - 1:
                    print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached.")

                if new_on_page == 0:
                    print(f"[{_SITE_ID}] All items on page {page_num} already seen; stopping section.")
                    break

                time.sleep(1.0)

        return saved
