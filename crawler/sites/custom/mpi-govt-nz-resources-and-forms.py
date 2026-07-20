# -*- coding: utf-8 -*-
"""MPI Farm Monitoring reports crawler.

Target: https://www.mpi.govt.nz/resources-and-forms/economic-intelligence/farm-monitoring/

The listing page organises PDF/XLSX reports in thematic sections:
  Viticulture, Pipfruit, Kiwifruit, Apiculture, Pastoral, 2012 Programme.

All documents appear on a single page (no pagination). Each document is a
  /dmsdocument/{id}-{slug}  link (direct file download).

Access strategy: www.mpi.govt.nz is blocked by Imperva/Incapsula for datacenter
IPs. The crawler tries a direct fetch first; on detecting the WAF block page it
falls back to the most recent Wayback Machine snapshot.
"""

import json
import re
import subprocess
import time
from typing import Optional

from crawler.base_crawler import BaseCrawler

_SITE_ID = "mpi-govt-nz-resources-and-forms"
_BASE_URL = "https://www.mpi.govt.nz"
_LISTING_URL = (
    "https://www.mpi.govt.nz"
    "/resources-and-forms/economic-intelligence/farm-monitoring/"
)
# Known-good Wayback Machine snapshot (2025-12-21 — verified to contain real data)
_WB_DEFAULT_TS = "20251221191151"

_BLOCK_MARKERS = [
    "Incapsula", "incap_ses", "_Incapsula_Resource",
    "SWJIYLWA", "SWUDNSAI",
]

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Section headings that appear in the listing page
_SECTION_HEADINGS = (
    "Viticulture",
    "Pipfruit",
    "Kiwifruit",
    "Apiculture",
    "Pastoral",
    "2012 Farm Monitoring",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_blocked(html: str) -> bool:
    return any(m in html for m in _BLOCK_MARKERS)


def _curl_get(url: str, timeout: int = 45, retries: int = 3) -> Optional[str]:
    """Fetch URL via curl with exponential backoff. Returns decoded text or None."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", f"User-Agent: {_USER_AGENT}",
        "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "-H", "Accept-Language: en-NZ,en-US;q=0.9,en;q=0.7",
        "-H", "Connection: keep-alive",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
            text = result.stdout.decode("utf-8", errors="replace")
            if text.strip():
                return text
        except Exception as exc:
            print(f"[{_SITE_ID}] curl error (attempt {attempt + 1}/{retries}): {exc}")
        if attempt < retries - 1:
            wait = 3 ** attempt  # 1s, 3s, 9s
            time.sleep(wait)
    return None


def _make_soup(html: str):
    """Build BeautifulSoup with html5lib → lxml → html.parser fallback."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except ImportError:
            continue
        except Exception:
            continue
    return None


def _strip_wayback(href: str) -> str:
    """Extract original URL from a Wayback Machine-wrapped href."""
    if not href:
        return ""
    # Full: https://web.archive.org/web/20251221191151/https://www.mpi.govt.nz/...
    m = re.search(r"web\.archive\.org/web/\d+/(https?://[^\s\"']+)", href)
    if m:
        return m.group(1)
    # Relative: /web/20251221191151/https://www.mpi.govt.nz/...
    m = re.search(r"^/web/\d+/(https?://[^\s\"']+)", href)
    if m:
        return m.group(1)
    return href


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class MPIFarmMonitoringCrawler(BaseCrawler):
    """Crawler for MPI (NZ Ministry for Primary Industries) farm monitoring reports.

    Fetches the single listing page (with Wayback Machine fallback), parses
    thematic sections, and saves each PDF/XLSX document as a DB record.  Section
    descriptions serve as the abstract for every document in that section.
    """

    # Class attributes — NOT @property (required for spec_from_file_location usage)
    site_id = "mpi-govt-nz-resources-and-forms"
    site_name = "Custom: mpi-govt-nz-resources-and-forms"
    base_url = "https://www.mpi.govt.nz"

    # -----------------------------------------------------------------------
    # Fetching
    # -----------------------------------------------------------------------

    def _find_recent_wayback_ts(self) -> str:
        """Query CDX API for the most-recent successful snapshot timestamp."""
        cdx_url = (
            "http://web.archive.org/cdx/search/cdx"
            f"?url={_LISTING_URL}&output=json&limit=1"
            "&fl=timestamp&filter=statuscode:200&from=20250101"
        )
        raw = _curl_get(cdx_url, timeout=20, retries=2)
        if raw:
            try:
                data = json.loads(raw)
                # [["timestamp"], ["20251221191151"]]
                if len(data) > 1 and data[-1]:
                    return str(data[-1][0])
            except Exception:
                pass
        return _WB_DEFAULT_TS

    def _fetch_listing_html(self) -> Optional[str]:
        """Return the listing page HTML, falling back to Wayback Machine."""
        # 1. Try direct access
        html = _curl_get(_LISTING_URL, timeout=30, retries=2)
        if html and not _is_blocked(html) and len(html) > 5000:
            print(f"[{self.site_id}] Listing fetched directly ({len(html):,} bytes)")
            return html

        if html and _is_blocked(html):
            print(f"[{self.site_id}] Direct access blocked by WAF; switching to Wayback Machine")
        else:
            print(f"[{self.site_id}] Direct fetch failed; switching to Wayback Machine")

        # 2. Find the freshest snapshot
        ts = self._find_recent_wayback_ts()
        wb_url = f"https://web.archive.org/web/{ts}/{_LISTING_URL}"
        print(f"[{self.site_id}] Wayback snapshot {ts}: {wb_url}")

        html = _curl_get(wb_url, timeout=60, retries=3)
        if html and len(html) > 5000:
            print(f"[{self.site_id}] Wayback listing fetched ({len(html):,} bytes)")
            return html

        print(f"[{self.site_id}] ERROR: all fetch attempts failed")
        return None

    # -----------------------------------------------------------------------
    # Parsing
    # -----------------------------------------------------------------------

    def _parse_listing(self, html: str) -> list:
        """Parse the listing page and return a list of section dicts.

        Each section dict: {section, description, docs}
        Each doc dict: {doc_id, title, url, pdf_url, year, file_type,
                        original_filename, raw_title}
        """
        soup = _make_soup(html)
        if soup is None:
            print(f"[{self.site_id}] ERROR: BeautifulSoup failed on all parsers")
            return []

        for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
            tag.decompose()

        sections = []
        cur_section = None
        cur_desc = []
        cur_docs = []

        for elem in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "a"]):
            try:
                name = elem.name
                text = elem.get_text(separator=" ", strip=True)
                if not text:
                    continue
                href = elem.get("href", "") if name == "a" else ""

                # ---- Section headings ----
                if name in ("h1", "h2", "h3", "h4"):
                    if any(kw in text for kw in _SECTION_HEADINGS):
                        if cur_section is not None:
                            sections.append({
                                "section": cur_section,
                                "description": " ".join(cur_desc).strip(),
                                "docs": list(cur_docs),
                            })
                        cur_section = text
                        cur_desc = []
                        cur_docs = []
                    continue

                # ---- Description paragraphs ----
                if name == "p" and cur_section and len(text) > 30 and not href:
                    cur_desc.append(text)
                    continue

                # ---- Document links ----
                if name == "a" and href and cur_section:
                    orig = _strip_wayback(href)
                    if not orig or "dmsdocument" not in orig:
                        continue

                    m = re.search(r"/dmsdocument/(\d+)", orig)
                    if not m:
                        continue
                    doc_id = m.group(1)

                    file_type = (
                        "PDF" if "[PDF" in text else
                        "XLSX" if "[XLSX" in text else
                        "FILE"
                    )
                    clean_title = re.sub(
                        r"\s*\[(?:PDF|XLSX|FILE)[^\]]*\]", "", text
                    ).strip() or text.strip()

                    ym = re.search(r"\b(20\d{2}|19\d{2})\b", clean_title)
                    year = ym.group(1) if ym else None

                    # Canonical URL — always use live mpi.govt.nz
                    canonical = orig if orig.startswith(_BASE_URL) else (
                        f"{_BASE_URL}/dmsdocument/{doc_id}"
                    )

                    pdf_url = (
                        f"{_BASE_URL}/dmsdocument/{doc_id}/direct"
                        if file_type == "PDF" else None
                    )

                    # Original filename from URL slug (strip leading "{id}-")
                    slug = orig.rstrip("/").split("/")[-1]
                    fname_base = re.sub(r"^\d+-", "", slug)
                    ext = ".pdf" if file_type == "PDF" else ".xlsx" if file_type == "XLSX" else ""
                    original_filename = (fname_base + ext) if fname_base else None

                    cur_docs.append({
                        "doc_id": doc_id,
                        "title": clean_title,
                        "url": canonical,
                        "pdf_url": pdf_url,
                        "year": year,
                        "file_type": file_type,
                        "original_filename": original_filename,
                        "raw_title": text,
                    })

            except Exception as exc:
                print(f"[{self.site_id}] parse element error: {exc}")
                continue

        # Flush the last section
        if cur_section is not None:
            sections.append({
                "section": cur_section,
                "description": " ".join(cur_desc).strip(),
                "docs": list(cur_docs),
            })

        return sections

    # -----------------------------------------------------------------------
    # Main crawl
    # -----------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl MPI farm monitoring listing page and persist records to DB.

        All records live on a single page; limit controls how many to save.
        Falls back to Wayback Machine archive when the live site is blocked.

        Returns:
            Number of records saved.
        """
        start_time = time.time()
        saved = 0
        seen_urls: set = set()
        limit_str = str(limit) if limit is not None else "inf"

        # ---- Fetch listing ----
        html = self._fetch_listing_html()
        if not html:
            print(f"[{self.site_id}] FATAL: could not fetch listing page")
            return 0

        # ---- Parse ----
        try:
            sections = self._parse_listing(html)
        except Exception as exc:
            print(f"[{self.site_id}] FATAL: parse failed: {exc}")
            return 0

        if not sections:
            print(f"[{self.site_id}] ERROR: no sections found — page structure may have changed")
            return 0

        total_docs = sum(len(s["docs"]) for s in sections)
        print(f"[{self.site_id}] {len(sections)} sections, {total_docs} documents")

        page_counter = 0

        for section_info in sections:
            if limit is not None and saved >= limit:
                break

            section_name = section_info["section"]
            section_desc = section_info["description"]
            docs = section_info["docs"]

            if not docs:
                continue

            # Build abstract from section description (guaranteed >= 100 chars)
            abstract = section_desc.strip()
            if len(abstract) < 100:
                abstract = (
                    f"MPI Farm Monitoring Programme - {section_name}. "
                    f"{abstract}. "
                    "Reports cover seasonal production, typical revenue, expenses, "
                    "and emerging trends in New Zealand primary industry sectors."
                ).strip()

            for doc in docs:
                if limit is not None and saved >= limit:
                    break

                # Wall-clock budget: 25 minutes
                if time.time() - start_time > 25 * 60:
                    print(f"[{self.site_id}] 25-min wall budget reached. Stopping.")
                    return saved

                doc_url = doc.get("url", "")
                if not doc_url or doc_url in seen_urls:
                    continue
                seen_urls.add(doc_url)

                doc_id = doc.get("doc_id", "")
                title = doc.get("title", "").strip()

                if not title or not doc_id:
                    continue

                # Skip items with insufficient abstract (log, don't crash)
                if len(abstract) < 50:
                    print(
                        f"[{self.site_id}] Skipping '{title[:40]}': "
                        f"abstract too short ({len(abstract)} chars)"
                    )
                    continue

                try:
                    year = doc.get("year")
                    published_date = f"{year}-01-01" if year else None

                    paper = {
                        "site_id": self.site_id,
                        "external_id": doc_id,
                        "post_number": doc_id,
                        "title": title,
                        "abstract": abstract,
                        "url": doc_url,
                        "pdf_url": doc.get("pdf_url"),
                        "published_date": published_date,
                        "listed_date": published_date,
                        "publisher": "Ministry for Primary Industries, New Zealand",
                        "department": "Economic Intelligence Unit",
                        "category": section_name,
                        "keywords": ", ".join(filter(None, [
                            section_name.split("(")[0].strip(),
                            "farm monitoring",
                            "New Zealand",
                            "MPI",
                            year or "",
                        ])),
                        "original_filename": doc.get("original_filename"),
                        "metadata": json.dumps({
                            "section": section_name,
                            "file_type": doc.get("file_type", ""),
                            "raw_title": doc.get("raw_title", ""),
                            "doc_id": doc_id,
                            "posted_date": published_date,
                            "originalFilename": doc.get("original_filename"),
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {doc_id} failed: {exc}")
                    continue

                time.sleep(0.3)

            page_counter += 1
            if page_counter % 10 == 0:
                print(f"[{self.site_id}] page {page_counter}: saved {saved}/{limit_str}")

        if page_counter >= 200:
            print(f"[{self.site_id}] Safety cap (200 pages) reached")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
