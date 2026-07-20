# -*- coding: utf-8 -*-
"""NETL DOE Advanced Search (advsearch?tid=107) crawler.

Source: https://netl.doe.gov/advsearch?tid=107
Strategy:
  - Parse the Drupal 9 Views HTML table (class="solicitation-row") for Publication entries.
  - Each row has: title link, optional PDF link, upload date.
  - Abstract is extracted from the PDF via pdftotext (node detail pages have no body text).
  - Items without a PDF, or whose extracted text is <100 chars, are logged and skipped.
  - The site shows all ~34 publications on a single default page (no JS pagination).
    We loop with page=N anyway for robustness; deduplication via seen_urls halts naturally.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Absolute import — spec_from_file_location has no package context, but the test
# does sys.path.insert(0, '.') before loading this file.
from crawler.base_crawler import BaseCrawler


_BASE_URL = "https://netl.doe.gov"
_LIST_URL  = "https://netl.doe.gov/advsearch"
_TID       = "107"
_MIN_ABSTRACT = 100   # chars; skip & log below this threshold


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _curl_get(url: str, *, timeout: int = 45) -> bytes | None:
    """GET via curl with TLS + retry.  Returns raw bytes or None after 3 failures."""
    cmd = [
        "curl", "-sk", "--tls-max", "1.3", "-L",
        "--max-time", str(timeout),
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        url,
    ]
    for attempt in range(3):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
            if r.stdout:
                return r.stdout
        except Exception as exc:
            print(f"[netl-doe-gov-advsearch] curl attempt {attempt + 1}/3 error for {url}: {exc}")
        if attempt < 2:
            time.sleep((attempt + 1) ** 2)   # 1 s, 4 s
    print(f"[netl-doe-gov-advsearch] All 3 curl attempts failed for {url}")
    return None


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

def _parse_bs(raw: bytes):
    """Parse HTML bytes via html5lib → lxml → html.parser fallback chain."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# PDF extraction
# ---------------------------------------------------------------------------

def _extract_pdf_text(pdf_url: str) -> str:
    """Download PDF and extract plain text with pdftotext.

    Returns the extracted string (may be very long) or '' on failure.
    Writes to a temp file because pdftotext needs a seekable file.
    """
    raw = _curl_get(pdf_url, timeout=90)
    if not raw:
        return ""
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(raw)
            tmp_path = f.name
        r = subprocess.run(
            ["pdftotext", "-layout", tmp_path, "-"],
            capture_output=True,
            timeout=120,
        )
        return r.stdout.decode("utf-8", errors="replace").strip()
    except Exception as exc:
        print(f"[netl-doe-gov-advsearch] pdftotext failed for {pdf_url}: {exc}")
        return ""
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------

def _parse_listed_date(raw: str) -> str | None:
    """Convert 'Sun, 03/01/2026 - 16:08' → '2026-03-01'.  None on failure."""
    if not raw:
        return None
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", raw)
    if m:
        mon, day, yr = m.groups()
        return f"{yr}-{mon}-{day}"
    return None


def _original_filename(pdf_url: str | None) -> str | None:
    """Extract the last path segment of a URL as the filename."""
    if not pdf_url:
        return None
    path = pdf_url.split("?")[0].rstrip("/")
    fname = path.rsplit("/", 1)[-1]
    return fname if fname else None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class NETLAdvSearchCrawler(BaseCrawler):
    """NETL DOE Publication Search — advsearch?tid=107."""

    site_id   = "netl-doe-gov-advsearch"
    site_name = "Custom: netl-doe-gov-advsearch"
    base_url  = "https://netl.doe.gov"

    def crawl(self, limit=None) -> int:  # noqa: C901
        saved     = 0
        seen_urls: set[str] = set()
        start_ts  = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        # Loop over Drupal "page" parameters.  In practice all ~34 items appear
        # on the first (default) fetch; subsequent pages return the same URLs
        # (different sort order) and are halted by deduplication.
        for page in range(200):

            # 25-minute wall-clock budget
            if time.time() - start_ts > 25 * 60:
                print(f"[{self.site_id}] 25-min budget reached at list-page {page}. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            # page=0 in our loop → default URL (newest-first, no &page= param)
            # page=N (N>0)       → ?page=N-1  (Drupal's pager param, 0-indexed)
            if page == 0:
                list_url = f"{_LIST_URL}?tid={_TID}"
            else:
                list_url = f"{_LIST_URL}?tid={_TID}&page={page}"

            raw = _curl_get(list_url)
            if not raw:
                print(f"[{self.site_id}] Could not fetch list-page {page}. Stopping.")
                break

            try:
                soup = _parse_bs(raw)
            except Exception as exc:
                print(f"[{self.site_id}] HTML parse error on list-page {page}: {exc}. Stopping.")
                break

            if soup is None:
                print(f"[{self.site_id}] HTML parse returned None on list-page {page}. Stopping.")
                break

            rows = soup.find_all("tr", class_="solicitation-row")
            if not rows:
                print(f"[{self.site_id}] No rows on list-page {page}. Done.")
                break

            new_this_page = 0

            for row in rows:
                if limit is not None and saved >= limit:
                    break

                try:
                    # ── title + detail URL ──────────────────────────────────
                    title_td = row.find("td", class_=re.compile(r"views-field-title"))
                    if not title_td:
                        continue
                    a_tag = title_td.find("a")
                    if not a_tag:
                        continue

                    title    = a_tag.get_text(strip=True)
                    rel_href = a_tag.get("href", "")
                    if not rel_href:
                        continue
                    detail_url = (
                        rel_href if rel_href.startswith("http")
                        else f"{_BASE_URL}{rel_href}"
                    )

                    # deduplication
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_this_page += 1

                    # ── PDF link ────────────────────────────────────────────
                    file_td = row.find("td", class_=re.compile(r"views-field-field-file"))
                    pdf_url: str | None = None
                    if file_td:
                        pdf_a = file_td.find("a")
                        if pdf_a:
                            pdf_href = pdf_a.get("href", "")
                            if pdf_href:
                                pdf_url = (
                                    pdf_href if pdf_href.startswith("http")
                                    else f"{_BASE_URL}{pdf_href}"
                                )

                    # ── upload date ─────────────────────────────────────────
                    date_td  = row.find("td", class_=re.compile(r"views-field-created"))
                    raw_date = date_td.get_text(strip=True) if date_td else ""
                    listed_date = _parse_listed_date(raw_date)

                    # ── external_id / post_number from node ID ──────────────
                    m_node      = re.search(r"/node/(\d+)", rel_href)
                    external_id = (
                        m_node.group(1) if m_node
                        else re.sub(r"[^a-z0-9-]", "-", rel_href.strip("/").lower())
                    )
                    post_number = m_node.group(1) if m_node else None

                    # ── abstract from PDF ───────────────────────────────────
                    time.sleep(self._delay)
                    abstract = ""
                    if pdf_url:
                        abstract = _extract_pdf_text(pdf_url)
                        if abstract:
                            abstract = abstract[:8000]   # cap; first 8 k chars covers the overview

                    if len(abstract) < _MIN_ABSTRACT:
                        print(
                            f"[{self.site_id}] Skipping '{title[:60]}' — "
                            f"abstract too short ({len(abstract)} chars, need ≥{_MIN_ABSTRACT})"
                        )
                        continue

                    # ── assemble paper dict ─────────────────────────────────
                    ofname = _original_filename(pdf_url)
                    paper  = {
                        "site_id":           self.site_id,
                        "external_id":       external_id,
                        "post_number":       post_number,
                        "title":             title,
                        "abstract":          abstract,
                        "published_date":    listed_date,
                        "posted_date":       listed_date,
                        "url":               detail_url,
                        "pdf_url":           pdf_url,
                        "original_filename": ofname,
                        "authors":           None,
                        "publisher":         "National Energy Technology Laboratory (NETL); U.S. Department of Energy",
                        "department":        None,
                        "journal":           None,
                        "keywords":          None,
                        "category":          None,
                        "doi":               None,
                        "metadata": json.dumps({
                            "posted_date":      raw_date,        # raw form for adapter auto-extract
                            "originalFilename": ofname,
                            "tid":              _TID,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] Item failed: {exc}; skipping to next")
                    continue

            # progress log every 10 pages
            if page > 0 and page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            if new_this_page == 0:
                print(f"[{self.site_id}] No new items on list-page {page}. Done.")
                break

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
