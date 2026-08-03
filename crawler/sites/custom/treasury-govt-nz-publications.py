# -*- coding: utf-8 -*-
"""The Treasury New Zealand (treasury.govt.nz) publications crawler.

Covers all 39 uncollected "뉴질랜드" rows under ``www.treasury.govt.nz`` from
``scripts/audit/uncollected_probe.csv``: 28 yearly Budget listing pages
(``publications/budgets/budget-YYYY``), 8 ``publications/search?f[0]=...``
filtered-category search pages, the JEL-classification research index, and
the corporate annual-reports listing. All are served by the same Drupal 9
CMS/theme, so one crawler + one hard-coded ``SECTIONS`` list (URL, category
label) covers every page.

Site is NOT behind Cloudflare/WAF — plain ``curl_cffi`` (layer 1 of
``StealthSession``) succeeds on every page tested. ``StealthSession`` is
still used (matching project convention / other custom crawlers) so a
future block is handled gracefully via its playwright/requests fallback.

Listing pages (both budget-year pages and search-result pages) share one
structure: ``div.node.slat`` items, each
``<div class="slat node ..."><h3 class="slat__title"><a href="...">Title
</a></h3><div class="slat__date ..."><time datetime="ISO">...</time></div>
</div>``, paginated via ``&page=N`` (0-indexed, ~30 items/page, next-page
affordance at ``.pager__item--next a[href]``). The one exception is the
JEL-classification index (``research-and-commentary/a-z/jel``), which is a
pure A-Z taxonomy index page with no ``.node.slat`` items of its own (its
26 ``?code=X`` sub-pages are out of scope here — the probe CSV only lists
the parent URL) — it yields 0 documents and the crawler moves on, which is
expected/logged, not an error.

Detail pages: title is the bare ``<h1>`` text. Abstract/summary comes from
the ``<meta name="description">`` tag (a short, human-written blurb — the
page body itself is metadata fields, not prose). Published date is read
from ``.resource__field-issue-date time[datetime]`` (ISO, ``YYYY-MM-
DDTHH:MM:SSZ``) — falls back to the listing's own ``<time>`` when a detail
page lacks it. A PDF, when present, is the first ``a[href$=".pdf"]`` inside
``<main>`` (files live under ``/sites/default/files/...`` — same-origin,
no special headers needed, downloaded via the shared
``crawler.pdf_downloader``).
"""

from __future__ import annotations

import html as html_module
import os
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
    """Get normalized text from a bs4 element/attr/str."""
    if value is None:
        return ""
    if hasattr(value, "get_text"):
        text = value.get_text(" ", strip=True)
    else:
        text = str(value)
    text = html_module.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


class TreasuryGovtNzPublicationsCrawler(BaseCrawler):
    """Crawls treasury.govt.nz Budget/search/annual-report listing sections."""

    site_id = "treasury-govt-nz-publications"
    site_name = "The Treasury New Zealand — Publications"
    base_url = "https://www.treasury.govt.nz"

    # The 39 uncollected treasury.govt.nz rows (시트명 contains "뉴질랜드")
    # from scripts/audit/uncollected_probe.csv: (list URL, category label).
    # Search-page category labels were read from each page's active facet
    # chip (``a.is-active .facet-item__value``) since the probe's captured
    # <title> is the generic "Publication search" for all 8 of them.
    SECTIONS = [
        ("https://www.treasury.govt.nz/publications/budgets/budget-2025", "Budget 2025"),
        ("https://www.treasury.govt.nz/publications/budgets/budget-2024", "Budget 2024"),
        ("https://www.treasury.govt.nz/publications/budgets/budget-2023", "Budget 2023"),
        ("https://www.treasury.govt.nz/publications/budgets/budget-2022", "Budget 2022"),
        ("https://www.treasury.govt.nz/publications/budgets/budget-2021", "Budget 2021"),
        ("https://www.treasury.govt.nz/publications/budgets/budget-2020", "Budget 2020"),
        ("https://www.treasury.govt.nz/publications/budgets/budget-2019", "Budget 2019"),
        ("https://www.treasury.govt.nz/publications/budgets/budget-2018", "Budget 2018"),
        ("https://www.treasury.govt.nz/publications/budgets/budget-2017", "Budget 2017"),
        ("https://www.treasury.govt.nz/publications/budgets/budget-2016", "Budget 2016"),
        ("https://www.treasury.govt.nz/publications/budgets/budget-2015", "Budget 2015"),
        ("https://www.treasury.govt.nz/publications/budgets/budget-2014", "Budget 2014"),
        ("https://www.treasury.govt.nz/publications/budgets/budget-2013", "Budget 2013"),
        ("https://www.treasury.govt.nz/publications/budgets/budget-2012", "Budget 2012"),
        ("https://www.treasury.govt.nz/publications/budgets/budget-2011", "Budget 2011"),
        ("https://www.treasury.govt.nz/publications/budgets/budget-2010", "Budget 2010"),
        ("https://www.treasury.govt.nz/publications/budgets/budget-2009", "Budget 2009"),
        ("https://www.treasury.govt.nz/publications/budgets/budget-2008", "Budget 2008"),
        ("https://www.treasury.govt.nz/publications/budgets/budget-2007", "Budget 2007"),
        ("https://www.treasury.govt.nz/publications/budgets/budget-2006", "Budget 2006"),
        ("https://www.treasury.govt.nz/publications/budgets/budget-2005", "Budget 2005"),
        ("https://www.treasury.govt.nz/publications/budgets/budget-2004", "Budget 2004"),
        ("https://www.treasury.govt.nz/publications/budgets/budget-2003", "Budget 2003"),
        ("https://www.treasury.govt.nz/publications/budgets/budget-2002", "Budget 2002"),
        ("https://www.treasury.govt.nz/publications/budgets/budget-2001", "Budget 2001"),
        ("https://www.treasury.govt.nz/publications/budgets/budget-2000", "Budget 2000"),
        ("https://www.treasury.govt.nz/publications/budgets/budget-1999", "Budget 1999"),
        ("https://www.treasury.govt.nz/publications/budgets/budget-1998", "Budget 1998"),
        ("https://www.treasury.govt.nz/publications/budgets/budget-1997", "Budget 1997"),
        ("https://www.treasury.govt.nz/publications/search?f%5B0%5D=publication_category%3A2697&issued_from=All&issued_to=All&search=&sort_by=issue_date&sort_order=DESC", "Budgets of the government"),
        ("https://www.treasury.govt.nz/publications/search?f%5B0%5D=publication_category%3A2698&issued_from=All&issued_to=All&search=&sort_by=issue_date&sort_order=DESC", "Commissioned reports"),
        ("https://www.treasury.govt.nz/publications/search?f%5B0%5D=publication_category%3A2701&issued_from=All&issued_to=All&search=&sort_by=issue_date&sort_order=DESC", "Data and models"),
        ("https://www.treasury.govt.nz/publications/search?f%5B0%5D=publication_category%3A2709&issued_from=All&issued_to=All&search=&sort_by=issue_date&sort_order=DESC", "Research and commentary"),
        ("https://www.treasury.govt.nz/publications/search?f%5B0%5D=resource_type%3A4415", "Working paper"),
        ("https://www.treasury.govt.nz/publications/research-and-commentary/a-z/jel", "Research papers by JEL classification"),
        ("https://www.treasury.govt.nz/publications/search?f%5B0%5D=resource_type%3A4476", "Treasury paper"),
        ("https://www.treasury.govt.nz/publications/search?f%5B0%5D=resource_type%3A15591", "Productivity Commission working paper"),
        ("https://www.treasury.govt.nz/publications/search?f%5B0%5D=resource_type%3A16111", "Productivity Commission research paper"),
        ("https://www.treasury.govt.nz/publications/corporate-documents/annual-reports", "Annual reports"),
    ]

    MAX_PAGES_PER_SECTION = 40
    MIN_ABSTRACT_CHARS = 20
    WALL_CLOCK_BUDGET_SEC = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    FETCH_RETRIES = 3

    def __init__(self, db_conn, delay=1.5):
        super().__init__(db_conn, delay=delay)
        self._stealth = StealthSession(playwright_timeout=60)

    # ------------------------------------------------------------------
    # Fetch helper — retry a few times on transient failures
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

            page = 0
            while True:
                if limit is not None and saved >= limit:
                    break
                if page >= self.MAX_PAGES_PER_SECTION:
                    print(f"[{self.site_id}] {category}: page cap "
                          f"({self.MAX_PAGES_PER_SECTION}) reached; next section")
                    break
                if time.monotonic() - start_time > self.WALL_CLOCK_BUDGET_SEC:
                    print(f"[{self.site_id}] wall-clock budget exhausted "
                          f"mid-section '{category}'")
                    break

                sep = "&" if "?" in section_url else "?"
                page_url = section_url if page == 0 else f"{section_url}{sep}page={page}"
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

                if not new_items and page > 0:
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
        for node in soup.select("div.node.slat"):
            a = node.select_one(".slat__title a[href]") or node.select_one("a[href]")
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
            time_tag = node.select_one("time[datetime]")
            if time_tag:
                dt_raw = (time_tag.get("datetime") or "").strip()
                if re.match(r"^\d{4}-\d{2}-\d{2}", dt_raw):
                    listed_date = dt_raw[:10]

            items.append({"url": url, "title": title, "listed_date": listed_date})
        return items

    def _has_next_page(self, soup):
        return soup.select_one(".pager__item--next a[href]") is not None

    # ------------------------------------------------------------------
    # Detail scraping
    # ------------------------------------------------------------------

    def _scrape_detail(self, item, category):
        html = self._fetch(item["url"])
        if html is None:
            print(f"[{self.site_id}] detail fetch failed: {item['url']}")
            return None
        soup = BeautifulSoup(html, "html.parser")

        title = ""
        h1 = soup.find("h1")
        if h1 is not None:
            title = _clean(h1)
        if not title:
            title = item["title"]
        if not title:
            print(f"[{self.site_id}] skipped (no title): {item['url']}")
            return None

        main = soup.find("main") or soup

        meta_desc = soup.find("meta", attrs={"name": "description"})
        abstract = _clean(meta_desc.get("content")) if meta_desc else ""
        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            print(f"[{self.site_id}] skipped (abstract too short, "
                  f"{len(abstract)} chars): {item['url']}")
            return None

        published_date = ""
        date_tag = soup.select_one(".resource__field-issue-date time[datetime]")
        if date_tag:
            dt_raw = (date_tag.get("datetime") or "").strip()
            if re.match(r"^\d{4}-\d{2}-\d{2}", dt_raw):
                published_date = dt_raw[:10]
        if not published_date:
            published_date = item.get("listed_date") or ""
        listed_date = item.get("listed_date") or published_date

        pdf_url = None
        for a in main.select("a[href]"):
            href = a.get("href") or ""
            if href.split("?", 1)[0].lower().endswith(".pdf"):
                pdf_url = urljoin(self.base_url, href)
                break

        author_el = soup.select_one(".resource__field-cassbase-author-corporate")
        publisher = _clean(author_el).replace("Corporate author :", "").strip() if author_el else ""
        publisher = publisher or "The Treasury New Zealand"

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
            "authors": publisher,
            "publisher": publisher,
            "journal": "The Treasury New Zealand",
            "category": category,
            "keywords": category,
            "metadata": None,
        }


# ---------------------------------------------------------------------------
# PDF download — treasury.govt.nz's ``/sites/default/files/...`` asset host
# is same-origin, unauthenticated and NOT behind Cloudflare/WAF (unlike
# government.se's contentassets host), so the generic
# ``crawler.pdf_downloader.download_pdf_for`` (plain ``requests``) works
# unmodified. This just loops it over this site's pending rows, mirroring
# the ``download_pdfs_for_site`` helper in ``government-se-publications.py``.
# ---------------------------------------------------------------------------

def download_pdfs_for_site(conn, site_id=None, limit=None, blob_root=None, delay=1.0):
    """Download pending PDFs for this crawler's documents. Returns (ok, failed)."""
    from crawler import pdf_downloader as _pdfdl

    site_id = site_id or TreasuryGovtNzPublicationsCrawler.site_id

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
        result = _pdfdl.download_pdf_for(conn, seq_id, pdf_url, blob_root=blob_root)
        if result.get("success"):
            ok += 1
            print(f"[{site_id}] pdf downloaded seq_id={seq_id} "
                  f"({result.get('size_bytes')} bytes)")
        else:
            failed += 1
            print(f"[{site_id}] pdf download failed seq_id={seq_id}: "
                  f"{result.get('error')}")
        time.sleep(delay)

    print(f"[{site_id}] pdf download done: {ok} ok, {failed} failed, {len(rows)} total")
    return ok, failed
