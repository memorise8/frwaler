# -*- coding: utf-8 -*-
"""Department of Health, Disability and Ageing (health.gov.au) crawler.

Covers the 1 usable uncollected "호주완료" health.gov.au row from
``scripts/audit/uncollected_probe.csv`` (``/resources/publications``); the
2nd row, a ``/node/44804?query=pdf...`` search results page, is not a
listing section and is skipped.

Not behind Cloudflare — plain ``curl_cffi`` (layer 1 of
``crawler.stealth_fetcher.StealthSession``) gets a clean 200. Uses a
health.gov.au-specific ("H" design system) theme, distinct from the
AU Design System / govCMS "views"/"teaser" theme shared by the other
crawlers in this batch — its listing/detail markup uses ``health-field``/
``au-callout`` classes rather than Drupal's ``field--name-*`` classes.

Listing: ``article.node--h_publication`` items, each with
``h3.au-display-md a[href]`` (title + detail link), a
``.health-metadata`` block holding a ``time[datetime]`` (ISO listed date)
+ publication type, and a final ``.health-field--label-hidden`` node
holding the plain-text summary (picked out as the *longest* of that
item's ``.health-field--label-hidden .health-field__item`` text nodes,
since an earlier one in the same item is just a cover-image alt wrapper
with no text). This section is large (~9,600 doc hint per the probe;
385 pager pages seen in testing) — ``MAX_PAGES_PER_SECTION`` caps a
single crawl run.

Detail pages: title is the page's own ``<h1>``. The file-download
attachment (PDF, sometimes Excel/Word instead) is
``article a.health-file__link[href]`` — filtered to ``.pdf`` specifically
since some publications only ship a spreadsheet. Full description lives
in the (second) ``.au-callout`` block that actually contains ``<p>`` text
(the first ``.au-callout`` on the page is just the file-download card and
has no ``<p>``). Publication date is confirmed via
``.health-field--inline time[datetime]`` but the listing's own ISO date
is used directly (equally reliable, avoids re-parsing).
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


class HealthGovAuPublicationsCrawler(BaseCrawler):
    """Crawls health.gov.au's publications listing section."""

    site_id = "health-gov-au-publications"
    site_name = "Department of Health, Disability and Ageing — Publications"
    base_url = "https://www.health.gov.au"

    # The 1 usable uncollected row (시트명 == "호주완료") from
    # scripts/audit/uncollected_probe.csv for www.health.gov.au (the 2nd
    # row, a /node/44804 search results page, is not a listing section
    # and is skipped).
    SECTIONS = [
        ("https://www.health.gov.au/resources/publications", "Publications"),
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
        for card in soup.select("article.node--h_publication"):
            a = card.select_one("h3 a[href]") or card.select_one("a[href]")
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
            time_tag = card.select_one("time[datetime]")
            if time_tag:
                listed_date = _parse_iso_date(time_tag.get("datetime") or "")

            pub_type = ""
            metadata = card.select_one(".health-metadata")
            if metadata is not None:
                fields = metadata.select(".health-field__item")
                # First field__item under health-metadata is the date; a
                # second, when present, is the publication type.
                texts = [_clean(f) for f in fields]
                texts = [t for t in texts if t]
                if texts:
                    pub_type = texts[-1] if len(texts) > 1 else ""

            # The description is the longest text among this card's
            # label-hidden health-field items (an earlier one is just a
            # cover-image alt wrapper with no text).
            candidates = [_clean(f) for f in card.select(".health-field--label-hidden .health-field__item")]
            summary = max(candidates, key=len) if candidates else ""

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
        article = soup.find("article") or soup

        title = _clean(article.find("h1") or soup.find("h1")) or item["title"]
        if not title:
            print(f"[{self.site_id}] skipped (no title): {item['url']}")
            return None

        # The first .au-callout on the page is just the file-download
        # card (no <p> text); the real description lives in a later one.
        abstract = ""
        for callout in article.select(".au-callout"):
            if callout.find("p") is not None:
                text = _clean(callout)
                if len(text) > len(abstract):
                    abstract = text
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

        pdf_url = None
        pdf_a = article.select_one('a.health-file__link[href$=".pdf"]')
        if pdf_a is None:
            pdf_a = article.select_one('a[href$=".pdf"], a[href*=".pdf?"]')
        if pdf_a is not None:
            pdf_url = urljoin(self.base_url, pdf_a.get("href") or "")

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
            "authors": "Department of Health, Disability and Ageing",
            "publisher": "Department of Health, Disability and Ageing",
            "journal": "Department of Health, Disability and Ageing (Australia)",
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

    site_id = site_id or HealthGovAuPublicationsCrawler.site_id

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
                headers={"Referer": "https://www.health.gov.au/"},
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
