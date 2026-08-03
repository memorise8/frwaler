# -*- coding: utf-8 -*-
"""Ministry of Health NZ (health.govt.nz) publications crawler.

Covers all 22 uncollected "뉴질랜드" rows under ``www.health.govt.nz`` from
``scripts/audit/uncollected_probe.csv``: 21 ``statistics-research/
statistics-and-data-sets/<topic>`` listing pages plus the site-wide
``/publications`` listing. All are served by the same Drupal theme, so one
crawler + one hard-coded ``SECTIONS`` list (URL, category label) covers
every page.

Investigation note on the task's HTTP405 claim: none of these 22 URLs
actually return HTTP405 — the probe CSV recorded a plain ``requests``/
``curl_cffi`` 403 "Just a moment..." Cloudflare challenge for exactly one
row (``.../suicide``), which is a transient/occasional challenge, not a
structural GET rejection. Live re-testing here got a clean 200 (via
``StealthSession`` layer 1, ``curl_cffi``) for all 22 URLs including
``/suicide``. (The domain that genuinely returns HTTP405 on every request —
including full headless-browser navigation — is ``justice.govt.nz``; see
``justice-govt-nz-publications.py``'s module docstring.) ``StealthSession``
(with its playwright fallback) is used here anyway, matching project
convention, so any future re-challenge is handled gracefully.

Listing pages share one structure: ``article.sector-resource`` items, each
``<article class="node sector-resource ..."><div class="node__content">
<h2 class="... list-item__title ..."><a href="...">Title</a></h2><div
class="field--name-field-issue-date ..."><time datetime="ISO">...</time>
</div><div class="field--name-body ...">...summary...</div></div></article>``,
paginated via ``?page=N`` (0-indexed, ~15-17 items/page), next-page
affordance at ``.pager__item--next a[href]``.

Detail pages: title is the bare ``<h1>`` text. Abstract comes from
``<meta name="description">`` (matches the listing's inline summary
paragraph). Published date is read from the first ``<time datetime="ISO">``
tag on the page (the "Publication date:" field). A PDF, when present, is
the first ``a[href$=".pdf"]`` inside ``<main>`` (files live under
``/system/files/...`` — same-origin, no special headers needed).
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


class HealthGovtNzPublicationsCrawler(BaseCrawler):
    """Crawls health.govt.nz statistics-topic and publications listing sections."""

    site_id = "health-govt-nz-publications"
    site_name = "Ministry of Health NZ — Publications"
    base_url = "https://www.health.govt.nz"

    # The 22 uncollected health.govt.nz rows (시트명 contains "뉴질랜드")
    # from scripts/audit/uncollected_probe.csv: (list URL, category label).
    SECTIONS = [
        ("https://www.health.govt.nz/statistics-research/statistics-and-data-sets/maori-health", "Māori health"),
        ("https://www.health.govt.nz/statistics-research/statistics-and-data-sets/mental-health", "Mental health"),
        ("https://www.health.govt.nz/statistics-research/statistics-and-data-sets/obesity", "Obesity"),
        ("https://www.health.govt.nz/statistics-research/statistics-and-data-sets/suicide", "Suicide"),
        ("https://www.health.govt.nz/statistics-research/statistics-and-data-sets/tobacco", "Tobacco"),
        ("https://www.health.govt.nz/statistics-research/statistics-and-data-sets/alcohol-use", "Alcohol use"),
        ("https://www.health.govt.nz/statistics-research/statistics-and-data-sets/asian-health", "Asian health"),
        ("https://www.health.govt.nz/statistics-research/statistics-and-data-sets/cancer", "Cancer"),
        ("https://www.health.govt.nz/statistics-research/statistics-and-data-sets/child-and-youth-health", "Child and youth health"),
        ("https://www.health.govt.nz/statistics-research/statistics-and-data-sets/disability", "Disability"),
        ("https://www.health.govt.nz/statistics-research/statistics-and-data-sets/drug-use", "Drug use"),
        ("https://www.health.govt.nz/statistics-research/statistics-and-data-sets/environmental-health", "Environmental health"),
        ("https://www.health.govt.nz/statistics-research/statistics-and-data-sets/fetal-and-infant-death", "Fetal and infant death"),
        ("https://www.health.govt.nz/statistics-research/statistics-and-data-sets/hospital-event", "Hospital event"),
        ("https://www.health.govt.nz/statistics-research/statistics-and-data-sets/maternity-and-newborn", "Maternity and newborn"),
        ("https://www.health.govt.nz/statistics-research/statistics-and-data-sets/mortality", "Mortality"),
        ("https://www.health.govt.nz/statistics-research/statistics-and-data-sets/nutrition", "Nutrition"),
        ("https://www.health.govt.nz/statistics-research/statistics-and-data-sets/older-peoples-health", "Older people's health"),
        ("https://www.health.govt.nz/statistics-research/statistics-and-data-sets/oral-health", "Oral health"),
        ("https://www.health.govt.nz/statistics-research/statistics-and-data-sets/problem-gambling", "Gambling harm"),
        ("https://www.health.govt.nz/statistics-research/statistics-and-data-sets/socioeconomic-deprivation", "Socioeconomic deprivation"),
        ("https://www.health.govt.nz/publications", "Publications"),
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
        main = soup.find("main") or soup
        for article in main.select("article.sector-resource"):
            a = article.select_one("h2 a[href]") or article.select_one("a[href]")
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
            time_tag = article.select_one("time[datetime]")
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
        date_tag = main.select_one("time[datetime]")
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

        publisher = "Ministry of Health NZ"

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
            "journal": "Ministry of Health NZ",
            "category": category,
            "keywords": category,
            "metadata": None,
        }


# ---------------------------------------------------------------------------
# PDF download — health.govt.nz's ``/system/files/...`` asset host is
# same-origin, unauthenticated and NOT behind Cloudflare/WAF, so the generic
# ``crawler.pdf_downloader.download_pdf_for`` (plain ``requests``) works
# unmodified. This just loops it over this site's pending rows, mirroring
# the ``download_pdfs_for_site`` helper in ``government-se-publications.py``.
# ---------------------------------------------------------------------------

def download_pdfs_for_site(conn, site_id=None, limit=None, blob_root=None, delay=1.0):
    """Download pending PDFs for this crawler's documents. Returns (ok, failed)."""
    from crawler import pdf_downloader as _pdfdl

    site_id = site_id or HealthGovtNzPublicationsCrawler.site_id

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
