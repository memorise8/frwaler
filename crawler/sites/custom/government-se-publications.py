# -*- coding: utf-8 -*-
"""Government Offices of Sweden (government.se) publications crawler.

Covers all 50 uncollected "스웨덴 완료" rows from
``scripts/audit/uncollected_probe.csv`` — 39 topic/document-type listing
pages under ``government-policy/``, ``reports/``, ``press-releases/``,
``legal-documents/``, ``information-material/`` etc, plus 11
``government-of-sweden/ministry-of-*`` ministry pages. All 50 live under
the single domain ``www.government.se`` and are served by the same CMS
template, so one crawler + one hard-coded ``SECTIONS`` list (derived from
the probe CSV) covers every section.

Site sits behind Cloudflare (plain ``requests``/``curl_cffi`` → 403 "Just a
moment..." on most pages, confirmed for ``government-policy/financial-markets/``
in the probe). Every HTML fetch goes through
``crawler.stealth_fetcher.StealthSession`` (its playwright fallback layer
passes the JS challenge); fetches are retried a few times since Cloudflare
occasionally re-challenges.

Listing pages share one structure: an ``ul.list--block`` of ``li`` items,
each ``<li><div class="sortcompact"><a href="...">Title</a><div
class="block--timeLinks"><p>Published <time datetime="YYYY-MM-DD">...
</time> · <a href="/tx/...">Type</a> from <a href="/tx/...">Ministry</a>
</p></div></div></li>``, paginated via ``?p=N`` (20 items/page, next-page
affordance at ``.nav--pagination__next``).

Detail pages: title is the ``<h1>``'s own text (a nested
``span.h1-vignette`` carries a reference number and must be stripped
first). Body/abstract lives in ``div.has-wordExplanation`` (full article
text — press releases, policy pages) or, when absent, the shorter
``div.media__body`` (used on report/publication pages that are mostly a
PDF with a one-paragraph summary). The article's own publication date is
most reliably read from the ``<meta name="DC.Date.Created" content="M/D/
YYYY">`` tag (the visible ``<time>`` in the masthead is NOT machine-
parseable — its ``datetime`` attribute duplicates the display text, e.g.
``datetime="18 June 2026"``). A PDF attachment, when present, is linked
from an ``<a href="/contentassets/<hash>/<slug>/">`` (occasionally
``/globalassets/...``) inside ``<main>`` — clicking it triggers a browser
download rather than navigating, and requesting it needs BOTH a Chrome TLS
fingerprint AND a same-origin ``Referer`` header (see
:func:`download_pdfs_for_site`) — plain ``requests`` (what
``crawler.pdf_downloader.download_pdf_for`` uses) gets a 403 even with the
right headers, so this crawler downloads its own PDFs instead of relying
on the generic downloader.
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


def _parse_dc_date(raw: str) -> str:
    """Parse the ``DC.Date.Created`` meta value (``M/D/YYYY``) to ISO."""
    if not raw:
        return ""
    try:
        return datetime.strptime(raw.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


class GovernmentSePublicationsCrawler(BaseCrawler):
    """Crawls government.se policy/publication/ministry listing sections."""

    site_id = "government-se-publications"
    site_name = "Government Offices of Sweden — Publications"
    base_url = "https://www.government.se"

    # The 50 uncollected Swedish rows (시트명 == "스웨덴 완료") from
    # scripts/audit/uncollected_probe.csv: (list URL, category label).
    SECTIONS = [
        ("https://www.government.se/information-material/", "Information material"),
        ("https://www.government.se/government-policy/care-for-older-people/", "Care for older people"),
        ("https://www.government.se/government-policy/childrens-rights/", "Children's rights"),
        ("https://www.government.se/government-policy/civil-defence/", "Civil defence"),
        ("https://www.government.se/government-policy/civil-society-and-sport/", "Civil society and sport"),
        ("https://www.government.se/government-policy/combating-terrorism", "Combating terrorism"),
        ("https://www.government.se/government-policy/the-constitution-of-sweden-and-personal-privacy/", "The Constitution of Sweden and personal privacy"),
        ("https://www.government.se/government-policy/culture/", "Culture"),
        ("https://www.government.se/government-policy/digital-policy/", "Digital policy"),
        ("https://www.government.se/government-policy/democracy-and-human-rights/", "Democracy and human rights"),
        ("https://www.government.se/government-policy/economic-policy/", "Economic policy"),
        ("https://www.government.se/government-policy/emergency-preparedness/", "Emergency preparedness"),
        ("https://www.government.se/government-policy/energy/", "Energy"),
        ("https://www.government.se/government-policy/enterprise-and-industry/", "Enterprise and industry"),
        ("https://www.government.se/government-policy/environment-and-climate/", "Environment and climate"),
        ("https://www.government.se/government-policy/financial-markets/", "Financial markets"),
        ("https://www.government.se/government-policy/foreign-and-security-policy/", "Foreign and security policy"),
        ("https://www.government.se/government-policy/gender-equality/", "Gender equality"),
        ("https://www.government.se/government-policy/higher-education-research-and-space/", "Higher education, research and space"),
        ("https://www.government.se/government-policy/innovation/", "Innovation"),
        ("https://www.government.se/government-policy/multilateral-cooperation/", "International development cooperation"),
        ("https://www.government.se/government-policy/international-law/", "International law"),
        ("https://www.government.se/government-policy/judicial-system/", "Judicial system"),
        ("https://www.government.se/government-policy/labour-law-and-work-environment/", "Labour law and work environment"),
        ("https://www.government.se/government-policy/labour-market/", "Labour market"),
        ("https://www.government.se/government-policy/medical-care/", "Medical care"),
        ("https://www.government.se/government-policy/migration-and-asylum/", "Migration and asylum"),
        ("https://www.government.se/government-policy/military-defence/", "Military defence"),
        ("https://www.government.se/government-policy/nordic-affairs/", "Nordic affairs"),
        ("https://www.government.se/government-policy/public-health/", "Public health"),
        ("https://www.government.se/government-policy/rural-affairs-agriculture-and-food-production/", "Rural affairs, agriculture and food production"),
        ("https://www.government.se/government-policy/social-services/", "Social services"),
        ("https://www.government.se/government-policy/state-owned-enterprises/", "State-owned enterprises"),
        ("https://www.government.se/government-policy/trade-and-investment-promotion/", "Trade and investment promotion"),
        ("https://www.government.se/government-policy/transport-and-infrastructure/", "Transport and infrastructure"),
        ("https://www.government.se/legal-documents/", "Legal documents"),
        ("https://www.government.se/reports/", "Reports"),
        ("https://www.government.se/international-development-cooperation-strategies/", "International development cooperation strategies"),
        ("https://www.government.se/press-releases/", "Press releases"),
        ("https://www.government.se/government-of-sweden/prime-ministers-office/", "Prime Minister's Office"),
        ("https://www.government.se/government-of-sweden/ministry-of-climate-and-enterprise/", "Ministry of Climate and Enterprise"),
        ("https://www.government.se/government-of-sweden/ministry-of-culture/", "Ministry of Culture"),
        ("https://www.government.se/government-of-sweden/ministry-of-defence/", "Ministry of Defence"),
        ("https://www.government.se/government-of-sweden/ministry-of-education-and-research/", "Ministry of Education and Research"),
        ("https://www.government.se/government-of-sweden/ministry-of-employment/", "Ministry of Employment"),
        ("https://www.government.se/government-of-sweden/ministry-of-finance/", "Ministry of Finance"),
        ("https://www.government.se/government-of-sweden/ministry-for-foreign-affairs/", "Ministry for Foreign Affairs"),
        ("https://www.government.se/government-of-sweden/ministry-of-health-and-social-affairs/", "Ministry of Health and Social Affairs"),
        ("https://www.government.se/government-of-sweden/ministry-of-justice/", "Ministry of Justice"),
        ("https://www.government.se/government-of-sweden/ministry-of-rural-affairs-and-infrastructure/", "Ministry of Rural Affairs and Infrastructure"),
    ]

    MAX_PAGES_PER_SECTION = 30
    MIN_ABSTRACT_CHARS = 40
    WALL_CLOCK_BUDGET_SEC = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    FETCH_RETRIES = 3

    def __init__(self, db_conn, delay=1.5):
        super().__init__(db_conn, delay=delay)
        self._stealth = StealthSession(playwright_timeout=60)

    # ------------------------------------------------------------------
    # Fetch helper — Cloudflare occasionally re-challenges, retry a few times
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

                page_url = section_url if page == 1 else f"{section_url}?p={page}"
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
        ul = soup.select_one("ul.list--block")
        if ul is None:
            return items
        for li in ul.find_all("li", recursive=False):
            a = li.select_one(".sortcompact > a[href]") or li.select_one("a[href]")
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
            time_tag = li.select_one("time[datetime]")
            if time_tag:
                dt_raw = (time_tag.get("datetime") or "").strip()
                if re.match(r"^\d{4}-\d{2}-\d{2}", dt_raw):
                    listed_date = dt_raw[:10]

            items.append({"url": url, "title": title, "listed_date": listed_date})
        return items

    def _has_next_page(self, soup):
        return soup.select_one(".nav--pagination__next a[href]") is not None

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
            vignette = h1.select_one(".h1-vignette")
            if vignette is not None:
                vignette.decompose()
            title = _clean(h1)
        if not title:
            title = item["title"]
        if not title:
            print(f"[{self.site_id}] skipped (no title): {item['url']}")
            return None

        main = soup.find("main") or soup

        body_el = main.select_one("div.has-wordExplanation") or main.select_one(".media__body")
        abstract = _clean(body_el) if body_el else ""
        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            meta_desc = soup.find("meta", attrs={"name": "description"})
            fallback = _clean(meta_desc.get("content")) if meta_desc else ""
            if len(fallback) > len(abstract):
                abstract = fallback
        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            print(f"[{self.site_id}] skipped (abstract too short, "
                  f"{len(abstract)} chars): {item['url']}")
            return None

        dc_date = soup.find("meta", attrs={"name": "DC.Date.Created"})
        published_date = _parse_dc_date(dc_date.get("content")) if dc_date else ""
        if not published_date:
            published_date = item.get("listed_date") or ""
        listed_date = item.get("listed_date") or published_date

        pdf_url = None
        for a in main.select("a[href]"):
            href = a.get("href") or ""
            if re.search(r"/(contentassets|globalassets)/", href):
                pdf_url = urljoin(self.base_url, href)
                break

        ministry_a = soup.select_one(".categories-text a[href^='/tx/']")
        publisher = _clean(ministry_a) if ministry_a else "Government Offices of Sweden"

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
            "journal": "Government Offices of Sweden",
            "category": category,
            "keywords": category,
            "metadata": None,
        }


# ---------------------------------------------------------------------------
# PDF download — government.se's contentassets/globalassets PDF host sits
# behind the same Cloudflare WAF as the HTML pages and additionally rejects
# requests without a same-origin Referer. Plain ``requests`` (used by
# ``crawler.pdf_downloader.download_pdf_for``) gets a 403 here even with the
# right headers — a Chrome TLS fingerprint is required. curl_cffi (already a
# project dependency via ``crawler.stealth_fetcher``) passes both checks.
# This reuses the same persistence primitives ``download_pdf_for`` uses
# (``crawler.blob_storage.save_pdf`` / ``crawler.db_libertree.update_document_pdf``)
# — only the HTTP transport differs.
# ---------------------------------------------------------------------------

def download_pdfs_for_site(conn, site_id=None, limit=None, blob_root=None, delay=1.0):
    """Download pending PDFs for this crawler's documents. Returns (ok, failed)."""
    from curl_cffi import requests as _creq

    from crawler import blob_storage as _blobs
    from crawler import db_libertree as _ldb

    site_id = site_id or GovernmentSePublicationsCrawler.site_id

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
                headers={"Referer": "https://www.government.se/"},
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
