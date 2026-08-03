# -*- coding: utf-8 -*-
"""Attorney-General's Department (ag.gov.au) publications crawler.

Covers 7 of the 8 uncollected "호주완료" ag.gov.au rows from
``scripts/audit/uncollected_probe.csv`` (the 8th, ``/search?query=pdf``, is
a generic search page, not a listing section, and is skipped). All 7 live
under the single domain ``www.ag.gov.au`` and are served by the same
govCMS/Drupal "views" theme, so one crawler + one hard-coded ``SECTIONS``
list covers every section.

Not behind Cloudflare — plain ``curl_cffi`` (layer 1 of
``crawler.stealth_fetcher.StealthSession``) gets a clean 200 on both
listing and detail pages, no playwright fallback needed in testing. Still
routed through ``StealthSession`` for resilience/consistency with other
crawlers in this batch.

Listing pages share one structure: ``div.view-content`` containing
``div.views-row`` items, each wrapping an
``article.node--type-publication`` with ``.search__title a[href]``
(title + detail link), ``.search__tags`` (publication type),
``.search__info time[datetime]`` (listed date) and ``.search__summary``
(short summary — used as abstract fallback when the detail page's own
body field is too short/absent). Pagination is 0-indexed via
``?page=N`` (page 1 has no query param), next-page affordance at
``.pager__item--next a[href]`` / ``a[rel="next"]``.

Detail pages: title is the page's own ``<h1>`` (no extra cleanup needed,
unlike government.se). Full abstract lives in ``.field--name-body`` scoped
under ``<main>`` (the same class also appears, empty, inside a hidden
header search-widget outside ``<main>`` — scoping avoids picking that up).
Publication date comes from ``.field--name-field-publication-date
time[datetime]``. PDF attachments (when present — most publication types
here are pure web pages with no attachment) are linked from
``.field--name-field-attachments a[type*="application/pdf"]``; the
attachments field commonly also holds a parallel ``.docx`` of the same
document, so the PDF is picked out explicitly by MIME type rather than by
first link.
"""

from __future__ import annotations

import html as html_module
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from bs4 import BeautifulSoup  # noqa: E402

from crawler.base_crawler import BaseCrawler  # noqa: E402
from crawler.stealth_fetcher import StealthSession  # noqa: E402


def _clean(value) -> str:
    """Get normalized text from a bs4 element/attr/str."""
    if value is None:
        return ""
    if hasattr(value, "get_text"):
        text = value.get_text(" ", strip=True)
    else:
        text = str(value)
    text = html_module.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_iso_date(raw: str) -> str:
    """Parse a ``datetime`` attribute (``YYYY-MM-DDTHH:MM:SSZ``) to ISO date."""
    if not raw:
        return ""
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", raw.strip())
    return m.group(1) if m else ""


class AgGovAuPublicationsCrawler(BaseCrawler):
    """Crawls ag.gov.au topic publications listing sections."""

    site_id = "ag-gov-au-publications"
    site_name = "Attorney-General's Department — Publications"
    base_url = "https://www.ag.gov.au"

    # The 7 usable uncollected rows (시트명 == "호주완료") from
    # scripts/audit/uncollected_probe.csv for www.ag.gov.au (the 8th row,
    # a /search?query=pdf page, is not a listing section and is skipped).
    SECTIONS = [
        ("https://www.ag.gov.au/integrity/publications", "Integrity"),
        ("https://www.ag.gov.au/international-relations/publications", "International Relations"),
        ("https://www.ag.gov.au/families-and-marriage/publications", "Families and Marriage"),
        ("https://www.ag.gov.au/rights-and-protections/publications", "Rights and Protections"),
        ("https://www.ag.gov.au/legal-system/publications", "Legal System"),
        ("https://www.ag.gov.au/crime/publications", "Crime"),
        ("https://www.ag.gov.au/national-security/publications", "National Security"),
    ]

    MAX_PAGES_PER_SECTION = 30
    MIN_ABSTRACT_CHARS = 40
    WALL_CLOCK_BUDGET_SEC = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
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

            page = 1
            while True:
                if limit is not None and saved >= limit:
                    break
                if page > self.MAX_PAGES_PER_SECTION:
                    print(f"[{self.site_id}] {category}: page cap "
                          f"({self.MAX_PAGES_PER_SECTION}) reached; next section")
                    break
                if time.monotonic() - start_time > self.WALL_CLOCK_BUDGET_SEC:
                    print(f"[{self.site_id}] wall-clock budget exhausted "
                          f"mid-section '{category}'")
                    break

                # Drupal views pager is 0-indexed; page 1 has no query param.
                page_url = section_url if page == 1 else f"{section_url}?page={page - 1}"
                html = self._fetch(page_url)
                if html is None:
                    print(f"[{self.site_id}] {category}: page {page} fetch "
                          f"failed; next section")
                    break

                soup = BeautifulSoup(html, "html.parser")
                items = self._parse_listing(soup)
                if not items:
                    print(f"[{self.site_id}] {category}: no list items at "
                          f"page {page}; next section")
                    break

                new_items = [it for it in items if it["url"] not in seen_urls]
                for it in new_items:
                    seen_urls.add(it["url"])

                for it in new_items:
                    if limit is not None and saved >= limit:
                        break
                    if time.monotonic() - start_time > self.WALL_CLOCK_BUDGET_SEC:
                        break
                    try:
                        time.sleep(self._delay)
                        paper = self._scrape_detail(it, category)
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

                if not new_items and page > 1:
                    print(f"[{self.site_id}] {category}: page {page} all "
                          f"duplicates; next section")
                    break

                if not self._has_next_page(soup):
                    break
                page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Listing parsing
    # ------------------------------------------------------------------

    def _parse_listing(self, soup):
        items = []
        content = soup.select_one("div.view-content")
        if content is None:
            return items
        for row in content.select(".views-row"):
            a = row.select_one(".search__title a[href]") or row.select_one("a[href]")
            if a is None:
                continue
            href = (a.get("href") or "").strip()
            if not href:
                continue
            url = urljoin(self.base_url, href)
            title = _clean(a)
            if not title:
                continue

            listed_date = ""
            time_tag = row.select_one(".search__info time[datetime]")
            if time_tag:
                listed_date = _parse_iso_date(time_tag.get("datetime") or "")

            pub_type = _clean(row.select_one(".search__tags"))
            summary = _clean(row.select_one(".search__summary"))

            items.append({
                "url": url,
                "title": title,
                "listed_date": listed_date,
                "pub_type": pub_type,
                "summary": summary,
            })
        return items

    def _has_next_page(self, soup):
        return (soup.select_one(".pager__item--next a[href]")
                or soup.select_one('a[rel="next"]')) is not None

    # ------------------------------------------------------------------
    # Detail scraping
    # ------------------------------------------------------------------

    def _scrape_detail(self, item, category):
        html = self._fetch(item["url"])
        if html is None:
            print(f"[{self.site_id}] detail fetch failed: {item['url']}")
            return None
        soup = BeautifulSoup(html, "html.parser")
        main = soup.find("main") or soup

        title = _clean(main.find("h1")) or item["title"]
        if not title:
            print(f"[{self.site_id}] skipped (no title): {item['url']}")
            return None

        body_el = main.select_one(".field--name-body")
        abstract = _clean(body_el) if body_el else ""
        if len(abstract) < self.MIN_ABSTRACT_CHARS and item.get("summary"):
            if len(item["summary"]) > len(abstract):
                abstract = item["summary"]
        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            meta_desc = soup.find("meta", attrs={"name": "description"})
            fallback = _clean(meta_desc.get("content")) if meta_desc else ""
            if len(fallback) > len(abstract):
                abstract = fallback
        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            print(f"[{self.site_id}] skipped (abstract too short, "
                  f"{len(abstract)} chars): {item['url']}")
            return None

        date_tag = main.select_one(".field--name-field-publication-date time[datetime]")
        published_date = _parse_iso_date(date_tag.get("datetime") or "") if date_tag else ""
        if not published_date:
            published_date = item.get("listed_date") or ""
        listed_date = item.get("listed_date") or published_date

        pdf_url = None
        pdf_a = main.select_one('.field--name-field-attachments a[type*="application/pdf"]')
        if pdf_a is None:
            pdf_a = main.select_one('a[href$=".pdf"], a[href*=".pdf?"]')
        if pdf_a is not None:
            pdf_url = urljoin(self.base_url, pdf_a.get("href") or "")

        pub_type = item.get("pub_type") or category
        keywords = f"{category} | {pub_type}" if pub_type and pub_type != category else category

        slug = item["url"].split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]

        return {
            "site_id": self.site_id,
            "external_id": slug,
            "post_number": slug,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": listed_date,
            "url": item["url"],
            "pdf_url": pdf_url,
            "original_filename": None,
            "authors": "Attorney-General's Department",
            "publisher": "Attorney-General's Department",
            "journal": "Attorney-General's Department (Australia)",
            "category": category,
            "keywords": keywords,
            "metadata": None,
        }


# ---------------------------------------------------------------------------
# PDF download — ag.gov.au's ``sites/default/files`` PDF host isn't behind
# Cloudflare, but plain ``requests`` (what
# ``crawler.pdf_downloader.download_pdf_for`` uses) hangs/times out on it in
# testing; a Chrome TLS fingerprint via curl_cffi connects cleanly. This
# reuses the same persistence primitives ``download_pdf_for`` uses
# (``crawler.blob_storage.save_pdf`` / ``crawler.db_libertree.update_document_pdf``)
# — only the HTTP transport differs.
# ---------------------------------------------------------------------------

def download_pdfs_for_site(conn, site_id=None, limit=None, blob_root=None, delay=1.0):
    """Download pending PDFs for this crawler's documents. Returns (ok, failed)."""
    from curl_cffi import requests as _creq

    from crawler import blob_storage as _blobs
    from crawler import db_libertree as _ldb

    site_id = site_id or AgGovAuPublicationsCrawler.site_id

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
                headers={"Referer": "https://www.ag.gov.au/"},
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
