# -*- coding: utf-8 -*-
"""Department of Defence (defence.gov.au) publications crawler.

Covers 4 of the 5 uncollected "호주완료" defence.gov.au rows from
``scripts/audit/uncollected_probe.csv`` (the 5th, ``/search?keywords=pdf``,
is a generic search page, not a listing section, and is skipped). All 4
live under the single domain ``www.defence.gov.au``.

Unlike the other AU government crawlers in this batch, these pages are
NOT paginated listings with separate detail pages — each is a single
static "resource hub" page where the PDF download link itself *is* the
document (annual reports, census reports, discipline reports, export
statistics), grouped under ``<h2>`` section headings. So there is no
listing/detail split here: one page fetch yields every document on that
page directly.

Two different markup patterns are used interchangeably across (and even
within) these pages, both must be handled:

  1. ``<a href="/sites/default/files/.../foo.pdf" class="dod-download-file">
     Foo Report 2024 (PDF, 1.01 MB)</a>``
  2. ``<def-download-file label="Foo Report 2024 (PDF, 1.01 MB)"
     link="/sites/default/files/.../foo.pdf">Foo Report 2024 (PDF, 1.01
     MB)</def-download-file>`` — a custom web component, attributes only
     (no nested real text to speak of beyond a duplicate of ``label``).

Not behind Cloudflare — plain ``curl_cffi`` (layer 1 of
``crawler.stealth_fetcher.StealthSession``) gets a clean 200.

PDF host (``sites/default/files``) needs a Chrome TLS fingerprint (plain
``requests`` hangs/times out in testing) — same as ``ag-gov-au-publications``.
"""

from __future__ import annotations

import html as html_module
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from bs4 import BeautifulSoup  # noqa: E402

from crawler.base_crawler import BaseCrawler  # noqa: E402
from crawler.stealth_fetcher import StealthSession  # noqa: E402


def _clean(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "get_text"):
        text = value.get_text(" ", strip=True)
    else:
        text = str(value)
    text = html_module.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _strip_pdf_suffix(label: str) -> str:
    """Strip a trailing ``(PDF, 1.23 MB)``-style size annotation from a title."""
    return re.sub(r"\s*\(PDF[^)]*\)\s*$", "", label, flags=re.IGNORECASE).strip()


class DefenceGovAuPublicationsCrawler(BaseCrawler):
    """Crawls defence.gov.au "accessing information" resource pages."""

    site_id = "defence-gov-au-publications"
    site_name = "Department of Defence (Australia) — Publications"
    base_url = "https://www.defence.gov.au"

    # The 4 usable uncollected rows (시트명 == "호주완료") from
    # scripts/audit/uncollected_probe.csv for www.defence.gov.au (the 5th
    # row, a /search?keywords=pdf page, is not a resource page and is
    # skipped). Each URL is a single static page — no pagination.
    SECTIONS = [
        ("https://www.defence.gov.au/about/accessing-information/annual-reports", "Annual reports"),
        ("https://www.defence.gov.au/about/accessing-information/defence-census", "Defence census"),
        ("https://www.defence.gov.au/about/accessing-information/defence-force-discipline-reports", "Defence Force Discipline reports"),
        ("https://www.defence.gov.au/about/accessing-information/export-permit-statistics", "Export permit statistics"),
    ]

    MIN_ABSTRACT_CHARS = 20
    WALL_CLOCK_BUDGET_SEC = 15 * 60
    FETCH_RETRIES = 3

    def __init__(self, db_conn, delay=1.5):
        super().__init__(db_conn, delay=delay)
        self._stealth = StealthSession(playwright_timeout=60)

    # ------------------------------------------------------------------
    # Fetch helper
    # ------------------------------------------------------------------

    def _fetch(self, url):
        for attempt in range(self.FETCH_RETRIES):
            html, info = self._stealth.fetch_html(url)
            if html and info.get("final_reason") == "ok":
                return html
            print(f"[{self.site_id}] fetch attempt {attempt + 1}/"
                  f"{self.FETCH_RETRIES} failed for {url}: {info.get('final_reason')}")
            if attempt < self.FETCH_RETRIES - 1:
                time.sleep(2 * (attempt + 1))
        return None

    # ------------------------------------------------------------------
    # Crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        start_time = time.monotonic()
        limit_label = str(limit) if limit is not None else "inf"

        for section_url, category in self.SECTIONS:
            if limit is not None and saved >= limit:
                break
            if time.monotonic() - start_time > self.WALL_CLOCK_BUDGET_SEC:
                print(f"[{self.site_id}] wall-clock budget exhausted; "
                      f"stopping before section '{category}'")
                break

            html = self._fetch(section_url)
            if html is None:
                print(f"[{self.site_id}] {category}: page fetch failed; skipping")
                continue

            soup = BeautifulSoup(html, "html.parser")
            page_title = _clean(soup.title).split("|")[0].strip() if soup.title else category
            items = self._parse_resources(soup)
            if not items:
                print(f"[{self.site_id}] {category}: no download links found")
                continue

            for it in items:
                if limit is not None and saved >= limit:
                    break
                if it["url"] in seen_urls:
                    continue
                seen_urls.add(it["url"])
                try:
                    paper = self._build_paper(it, category, page_title, section_url)
                    if paper is None:
                        continue
                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_label}: "
                          f"{paper['title'][:70]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item failed ({it['url']}): {exc}")
                    continue

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Parsing — walk the page in document order, tracking the nearest
    # preceding <h2> as the sub-category for each download link found.
    # ------------------------------------------------------------------

    def _parse_resources(self, soup):
        items = []
        current_h2 = ""
        for tag in soup.find_all(["h2", "a", "def-download-file"]):
            if tag.name == "h2":
                current_h2 = _clean(tag)
                continue
            if tag.name == "a":
                classes = tag.get("class") or []
                if "dod-download-file" not in classes:
                    continue
                href = (tag.get("href") or "").strip()
                label = _clean(tag)
            else:  # def-download-file web component
                href = (tag.get("link") or "").strip()
                label = (tag.get("label") or "").strip() or _clean(tag)

            if not href or ".pdf" not in href.lower():
                continue
            url = urljoin(self.base_url, href)
            title = _strip_pdf_suffix(label) or label
            if not title:
                continue
            items.append({"url": url, "title": title, "subcategory": current_h2})
        return items

    # ------------------------------------------------------------------
    # Build a document dict — no separate detail page exists; the PDF
    # link and its surrounding headings ARE the whole record.
    # ------------------------------------------------------------------

    def _build_paper(self, item, category, page_title, page_url):
        title = item["title"]
        subcategory = item.get("subcategory") or ""

        parts = [title]
        if subcategory and subcategory.lower() != title.lower():
            parts.append(subcategory)
        parts.append(f"{page_title} — Department of Defence (Australia).")
        abstract = " — ".join(parts[:2]) + " " + parts[-1] if len(parts) > 1 else parts[0]
        abstract = _clean(abstract)
        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            print(f"[{self.site_id}] skipped (abstract too short): {item['url']}")
            return None

        year_match = re.search(r"(19|20)\d{2}", title) or re.search(r"(19|20)\d{2}", subcategory)
        published_date = f"{year_match.group(0)}-01-01" if year_match else ""

        keywords = f"{category} | {subcategory}" if subcategory else category

        slug = item["url"].split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        slug = re.sub(r"\.pdf$", "", slug, flags=re.IGNORECASE)

        return {
            "site_id": self.site_id,
            "external_id": slug,
            "post_number": slug,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": published_date,
            "url": page_url,
            "pdf_url": item["url"],
            "original_filename": None,
            "authors": "Department of Defence (Australia)",
            "publisher": "Department of Defence (Australia)",
            "journal": "Department of Defence (Australia)",
            "category": category,
            "keywords": keywords,
            "metadata": None,
        }


# ---------------------------------------------------------------------------
# PDF download — same rationale as ag-gov-au-publications: not behind
# Cloudflare, but plain ``requests`` hangs on this host in testing; curl_cffi
# with a Chrome TLS fingerprint connects cleanly.
# ---------------------------------------------------------------------------

def download_pdfs_for_site(conn, site_id=None, limit=None, blob_root=None, delay=1.0):
    """Download pending PDFs for this crawler's documents. Returns (ok, failed)."""
    from curl_cffi import requests as _creq

    from crawler import blob_storage as _blobs
    from crawler import db_libertree as _ldb

    site_id = site_id or DefenceGovAuPublicationsCrawler.site_id

    query = (
        "SELECT seq_id, pdf_url FROM documents "
        "WHERE site_id = ? AND pdf_url IS NOT NULL AND pdf_url != '' "
        "AND (pdf_downloaded IS NULL OR pdf_downloaded = 0) ORDER BY seq_id"
    )
    params = [site_id]
    if limit:
        query += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(query, params).fetchall()
    print(f"[{site_id}] pdf download: {len(rows)} pending")

    ok = 0
    failed = 0
    for row in rows:
        seq_id = row["seq_id"]
        pdf_url = row["pdf_url"]
        try:
            resp = _creq.get(
                pdf_url,
                impersonate="chrome131",
                timeout=60,
                allow_redirects=True,
                headers={"Referer": "https://www.defence.gov.au/"},
            )
            if resp.status_code != 200 or not resp.content:
                failed += 1
                _ldb.update_document_pdf(conn, seq_id, downloaded=False, size_bytes=0, sha256="")
                print(f"[{site_id}] pdf download failed (HTTP {resp.status_code}) "
                      f"seq_id={seq_id}: {pdf_url}")
                continue
            _, size, sha = _blobs.save_pdf(seq_id, resp.content, root=blob_root)
            _ldb.update_document_pdf(conn, seq_id, downloaded=True, size_bytes=size, sha256=sha)
            ok += 1
            print(f"[{site_id}] pdf downloaded seq_id={seq_id} ({size} bytes)")
        except Exception as exc:
            failed += 1
            _ldb.update_document_pdf(conn, seq_id, downloaded=False, size_bytes=0, sha256="")
            print(f"[{site_id}] pdf download error seq_id={seq_id}: {exc}")
        time.sleep(delay)

    print(f"[{site_id}] pdf download done: {ok} ok, {failed} failed, {len(rows)} total")
    return ok, failed
