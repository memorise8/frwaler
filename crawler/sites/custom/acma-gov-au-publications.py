# -*- coding: utf-8 -*-
"""Australian Communications and Media Authority (acma.gov.au) crawler.

Covers 2 of the 3 uncollected "호주완료" acma.gov.au rows from
``scripts/audit/uncollected_probe.csv`` (the 3rd, ``/search?...``, is a
generic search page, not a listing section, and is skipped). Both live
under the single domain ``www.acma.gov.au`` and are served by the same
govCMS/Drupal "card" theme.

Not behind Cloudflare — plain ``curl_cffi`` (layer 1 of
``crawler.stealth_fetcher.StealthSession``) gets a clean 200.

Listing pages share one structure regardless of card variant
(``card-type-publication`` on ``/publications``, ``card-type-wide`` on
``/media-releases``): ``article.card`` items, each with
``.card-title-wrap a.card-link[href]`` / ``h3.card-title`` (title + link),
``time.card-date[datetime]`` (ISO listed date — used directly, no detail-
page date parsing needed) and, on the wide/media-release variant only,
``p.card-body`` (short summary). Pagination via ``?page=N`` (0-indexed,
page 1 has no query param), next-page affordance at
``.pager__item--next a[rel="next"]``.

Detail pages have two distinct content shapes depending on which section
the item came from:
  - Media releases: body text lives in ``.field--name-field-html``.
  - Publications (e.g. regulatory instructions): often no body field at
    all — just a PDF attachment under
    ``.field--name-field-publication-file a.file-title[href]`` — abstract
    falls back to that link's own text, then to the listing card summary,
    then to the page's meta description.
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
    if value is None:
        return ""
    if hasattr(value, "get_text"):
        text = value.get_text(" ", strip=True)
    else:
        text = str(value)
    text = html_module.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_iso_date(raw: str) -> str:
    if not raw:
        return ""
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", raw.strip())
    return m.group(1) if m else ""


class AcmaGovAuPublicationsCrawler(BaseCrawler):
    """Crawls ACMA publications and media-release listing sections."""

    site_id = "acma-gov-au-publications"
    site_name = "Australian Communications and Media Authority — Publications"
    base_url = "https://www.acma.gov.au"

    # The 2 usable uncollected rows (시트명 == "호주완료") from
    # scripts/audit/uncollected_probe.csv for www.acma.gov.au (the 3rd
    # row, a /search?... page, is not a listing section and is skipped).
    SECTIONS = [
        ("https://www.acma.gov.au/publications", "Publications"),
        ("https://www.acma.gov.au/media-releases", "Media releases"),
    ]

    MAX_PAGES_PER_SECTION = 30
    MIN_ABSTRACT_CHARS = 15
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
        for card in soup.select("article.card"):
            a = card.select_one(".card-title-wrap a[href]") or card.select_one("a.card-link[href]")
            if a is None:
                continue
            href = (a.get("href") or "").strip()
            if not href:
                continue
            url = urljoin(self.base_url, href)
            title = _clean(card.select_one(".card-title")) or _clean(a)
            if not title:
                continue

            listed_date = ""
            time_tag = card.select_one("time.card-date[datetime]") or card.select_one("time[datetime]")
            if time_tag:
                listed_date = _parse_iso_date(time_tag.get("datetime") or "")

            pub_type = _clean(card.select_one(".publication-type"))
            summary = _clean(card.select_one(".card-body") or card.select_one("p.card-body"))

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

        title = _clean(soup.find("h1")) or item["title"]
        if not title:
            print(f"[{self.site_id}] skipped (no title): {item['url']}")
            return None

        # Scope to the main content region — a sitewide announcement
        # banner (#block-storybook-acma-announcementblock) also carries a
        # ``.field--name-field-html`` node and would otherwise be picked
        # up as the abstract on pages with no real body text.
        main = soup.select_one("#block-storybook-acma-mainpagecontent") or soup.find("main") or soup
        abstract = _clean(main.select_one(".field--name-field-html"))

        pdf_url = None
        pdf_a = main.select_one(".field--name-field-publication-file a.file-title[href]")
        if pdf_a is None:
            pdf_a = main.select_one('a[href$=".pdf"], a[href*=".pdf?"]')
        if pdf_a is not None:
            pdf_url = urljoin(self.base_url, pdf_a.get("href") or "")
            if len(abstract) < self.MIN_ABSTRACT_CHARS:
                link_text = _clean(pdf_a)
                if len(link_text) > len(abstract):
                    abstract = link_text

        if len(abstract) < self.MIN_ABSTRACT_CHARS and item.get("summary"):
            if len(item["summary"]) > len(abstract):
                abstract = item["summary"]
        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            meta_desc = soup.find("meta", attrs={"name": "description"})
            fallback = _clean(meta_desc.get("content")) if meta_desc else ""
            if len(fallback) > len(abstract):
                abstract = fallback
        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            abstract = title

        published_date = item.get("listed_date") or ""
        listed_date = published_date

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
            "authors": "Australian Communications and Media Authority",
            "publisher": "Australian Communications and Media Authority",
            "journal": "Australian Communications and Media Authority",
            "category": category,
            "keywords": keywords,
            "metadata": None,
        }


# ---------------------------------------------------------------------------
# PDF download — same rationale as ag-gov-au-publications: not behind
# Cloudflare, but plain ``requests`` is unreliable on gov.au
# ``sites/default/files`` hosts in testing; curl_cffi with a Chrome TLS
# fingerprint connects cleanly.
# ---------------------------------------------------------------------------

def download_pdfs_for_site(conn, site_id=None, limit=None, blob_root=None, delay=1.0):
    """Download pending PDFs for this crawler's documents. Returns (ok, failed)."""
    from curl_cffi import requests as _creq

    from crawler import blob_storage as _blobs
    from crawler import db_libertree as _ldb

    site_id = site_id or AcmaGovAuPublicationsCrawler.site_id

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
                headers={"Referer": "https://www.acma.gov.au/"},
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
