# -*- coding: utf-8 -*-
"""National Health and Medical Research Council (nhmrc.gov.au) crawler.

Covers 2 of the 3 uncollected "호주완료" nhmrc.gov.au rows from
``scripts/audit/uncollected_probe.csv`` (the 3rd, ``/search?...``, is a
generic search page, not a listing section, and is skipped). Both live
under the single domain ``www.nhmrc.gov.au`` and are served by the same
govCMS/Drupal "masonry" theme.

Not behind Cloudflare — plain ``curl_cffi`` (layer 1 of
``crawler.stealth_fetcher.StealthSession``) gets a clean 200.

Listing pages share one structure: ``div.masonry-item.views-row`` items,
each with ``div.card-description h3 a[href]`` (title + detail link),
``div.card-description p`` (one or more summary paragraphs — used
directly as abstract, no detail-page body fetch strictly required but
still done for the more-precise publication date and PDF link) and
``ul.tags-list li time[datetime]`` (ISO listed date). Pagination via
``?page=N`` (0-indexed, page 1 has no query param), next-page affordance
at ``.pager__item--next a[href]``.

Detail pages: the page's only ``<h1>`` sits in a title banner *outside*
the ``<article class="node--type-publication">`` wrapper, so it's read
globally; abstract/PDF are scoped to that ``<article>`` to avoid several
unrelated ``.field--name-body`` nodes reused sitewide for footer/
acknowledgement/feedback-widget/social-links blocks (confirmed via a
direct fetch: the article's own body field is one of ~5 same-class nodes
on the page). PDF attachment link (when present) is
``.views-field-field-attachments a[href$=".pdf"]``.
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


class NhmrcGovAuPublicationsCrawler(BaseCrawler):
    """Crawls NHMRC publications and resources listing sections."""

    site_id = "nhmrc-gov-au-publications"
    site_name = "National Health and Medical Research Council — Publications"
    base_url = "https://www.nhmrc.gov.au"

    # The 2 usable uncollected rows (시트명 == "호주완료") from
    # scripts/audit/uncollected_probe.csv for www.nhmrc.gov.au (the 3rd
    # row, a /search?... page, is not a listing section and is skipped).
    SECTIONS = [
        ("https://www.nhmrc.gov.au/about-us/publications", "Publications"),
        ("https://www.nhmrc.gov.au/about-us/resources", "Resources"),
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
        for row in soup.select("div.masonry-item.views-row"):
            desc = row.select_one(".card-description")
            if desc is None:
                continue
            a = desc.select_one("h3 a[href]") or desc.select_one("a[href]")
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
            time_tag = desc.select_one("time[datetime]")
            if time_tag:
                listed_date = _parse_iso_date(time_tag.get("datetime") or "")

            summary_parts = [
                _clean(p) for p in desc.find_all("p", recursive=False)
            ]
            summary = " ".join(p for p in summary_parts if p)

            items.append({
                "url": url,
                "title": title,
                "listed_date": listed_date,
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

        # The article body field is one of several same-class
        # ``.field--name-body`` nodes reused sitewide (footer, feedback
        # widget, social links) — scope to the actual article to avoid
        # picking up the wrong one.
        article = soup.find("article") or soup
        abstract = _clean(article.select_one(".field--name-body"))
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

        # The "Downloads" attachments block sits in a views block *after*
        # (outside) </article>, not inside it — search page-wide.
        pdf_url = None
        pdf_a = soup.select_one('.views-field-field-attachments a[href$=".pdf"]')
        if pdf_a is None:
            pdf_a = article.select_one('a[href$=".pdf"], a[href*=".pdf?"]')
        if pdf_a is not None:
            pdf_url = urljoin(self.base_url, pdf_a.get("href") or "")

        published_date = item.get("listed_date") or ""
        listed_date = published_date

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
            "authors": "National Health and Medical Research Council",
            "publisher": "National Health and Medical Research Council",
            "journal": "National Health and Medical Research Council (Australia)",
            "category": category,
            "keywords": category,
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

    site_id = site_id or NhmrcGovAuPublicationsCrawler.site_id

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
                headers={"Referer": "https://www.nhmrc.gov.au/"},
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
