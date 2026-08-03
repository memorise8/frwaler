# -*- coding: utf-8 -*-
"""New Zealand Ministry of Justice (justice.govt.nz) "Find a publication"
crawler.

Covers all 17 uncollected "뉴질랜드" rows under ``www.justice.govt.nz`` from
``scripts/audit/uncollected_probe.csv`` — all 17 are
``/about/publication-finder/?Filter_Topic=<id>[&Filter_Topic_child=<id>]``
result pages for 6 top-level topics (Family / Lawyers & service providers /
Māori-Crown Relations / Policy / Cabinet and related material / Research &
data) and their children. Topic/child id → label mapping (used for the
``SECTIONS`` category names below, since the probe CSV's captured
``<title>`` for every one of these 17 rows is the generic "Human
Verification" block page — see next paragraph) was recovered from a 2022
Wayback Machine snapshot's ``data-terms-json`` filter-widget payload
(``web.archive.org/web/20220128024340/.../publication-finder/?Filter_Topic=38``),
cross-referenced against the ``<select id="Form_SearchForm_Filter_Topic">``
option list on the same snapshot.

**HTTP405 root cause (task's investigation ask) — CONFIRMED HARD BLOCK,
NOT RESOLVED:** every one of these 17 URLs is behind an AWS WAF Bot
Control rule in CAPTCHA-challenge mode: every live response (verified via
``curl_cffi`` Chrome-TLS-impersonation, plain ``requests``, AND a full
headless-Chromium ``playwright`` navigation with ``playwright_stealth``
applied, waiting up to 8s for any auto-resolving JS challenge) returns
``HTTP 405`` with ``X-Amzn-Waf-Action: captcha`` and a ``<title>Human
Verification</title>`` page requiring an actual CAPTCHA solve — this is
categorically different from a Cloudflare "Just a moment" JS challenge
(which ``StealthSession``'s playwright layer passes) and is NOT
auto-resolving no matter how long the headless browser waits. Same result
whether GET or POST, with or without a same-origin ``Referer`` /
pre-warmed cookies from ``justice.govt.nz/``. Other paths on the same
domain (``/``, ``/about/``) are NOT behind this rule and load fine — the
WAF rule specifically targets ``/about/publication-finder/*`` and
``/publications/*``. There is no unprotected alternate route to the same
per-topic filtered results (the site's ``sitemap.xml`` — itself
unprotected — only lists static CMS pages, not the dynamically-filtered
publication-finder result set). **Solving this requires either a
CAPTCHA-solving integration (out of scope — no such capability exists in
this toolchain) or a change in the WAF's risk scoring for this IP/session,
neither of which this crawler can address.** This crawler is written to
the same conventions as the other custom NZ crawlers (retry-wrapped
``StealthSession`` fetch, graceful "fetch failed → skip section, don't
crash" handling) so it activates automatically and safely if/when the
block ever lifts; as of this writing every test run saves 0 documents and
logs a fetch failure for every section, which is the expected/correct
behaviour given the block, not a code defect.

Listing/detail structure below is reconstructed from the 2022 Wayback
snapshot (unverifiable against the live, currently-inaccessible page —
treat as best-effort / subject to drift if the live site's markup has
since changed): results live in ``#SearchResults article``, each
``<article><a href="..."><header><span class="searchResultHeader">Title
[ <span>PDF</span>, size ]</span></header><p>Abstract</p></a></article>``.
Critically, the anchor's ``href`` in this taxonomy points directly at the
PDF asset (``/assets/Documents/Publications/<slug>.pdf``) rather than at
an intermediate detail page — there is no separate detail-page fetch here,
title/abstract/pdf_url are all read off the listing item itself.
Pagination is offset-based: ``&start=0,25,50,75,...`` (25 items/page).
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


class JusticeGovtNzPublicationsCrawler(BaseCrawler):
    """Crawls justice.govt.nz "Find a publication" topic/child result sections."""

    site_id = "justice-govt-nz-publications"
    site_name = "New Zealand Ministry of Justice — Find a publication"
    base_url = "https://www.justice.govt.nz"

    # The 17 uncollected justice.govt.nz rows (시트명 contains "뉴질랜드")
    # from scripts/audit/uncollected_probe.csv: (list URL, category label).
    # Labels resolved from Filter_Topic/Filter_Topic_child ids via the 2022
    # Wayback Machine snapshot's taxonomy JSON (see module docstring) since
    # every live/probe-captured <title> for these URLs is "Human
    # Verification" (AWS WAF captcha block page).
    SECTIONS = [
        ("https://www.justice.govt.nz/about/publication-finder/?Filter_Topic=38&Filter_Topic_child=638", "Family / Family violence"),
        ("https://www.justice.govt.nz/about/publication-finder/?Filter_Topic=38&Filter_Topic_child=762", "Family / Family Court"),
        ("https://www.justice.govt.nz/about/publication-finder/?Filter_Topic=38&Filter_Topic_child=133", "Family / Factsheets"),
        ("https://www.justice.govt.nz/about/publication-finder/?Filter_Topic=38&Filter_Topic_child=57", "Family / Guidelines-Practice Notes"),
        ("https://www.justice.govt.nz/about/publication-finder/?Filter_Topic=45&Filter_Topic_child=549", "Lawyers & service providers / Legal aid lawyers"),
        ("https://www.justice.govt.nz/about/publication-finder/?Filter_Topic=45&Filter_Topic_child=552", "Lawyers & service providers / Restorative justice"),
        ("https://www.justice.govt.nz/about/publication-finder/?Filter_Topic=45&Filter_Topic_child=558", "Lawyers & service providers / Community law centres"),
        ("https://www.justice.govt.nz/about/publication-finder/?Filter_Topic=45&Filter_Topic_child=736", "Lawyers & service providers / Family violence providers"),
        ("https://www.justice.govt.nz/about/publication-finder/?Filter_Topic=45&Filter_Topic_child=748", "Lawyers & service providers / Duty lawyers"),
        ("https://www.justice.govt.nz/about/publication-finder/?Filter_Topic=738", "Māori-Crown Relations"),
        ("https://www.justice.govt.nz/about/publication-finder/?Filter_Topic=39", "Policy"),
        ("https://www.justice.govt.nz/about/publication-finder/?Filter_Topic=765", "Cabinet and related material"),
        ("https://www.justice.govt.nz/about/publication-finder/?Filter_Topic=41&Filter_Topic_child=76", "Research & data / Data & statistics"),
        ("https://www.justice.govt.nz/about/publication-finder/?Filter_Topic=41&Filter_Topic_child=77", "Research & data / Evaluation reports"),
        ("https://www.justice.govt.nz/about/publication-finder/?Filter_Topic=41&Filter_Topic_child=754", "Research & data / Factsheets"),
        ("https://www.justice.govt.nz/about/publication-finder/?Filter_Topic=41&Filter_Topic_child=78", "Research & data / Forecasts"),
        ("https://www.justice.govt.nz/about/publication-finder/?Filter_Topic=41&Filter_Topic_child=79", "Research & data / Research reports"),
    ]

    PAGE_SIZE = 25
    MAX_PAGES_PER_SECTION = 40
    MIN_ABSTRACT_CHARS = 10
    WALL_CLOCK_BUDGET_SEC = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    FETCH_RETRIES = 3

    def __init__(self, db_conn, delay=1.5):
        super().__init__(db_conn, delay=delay)
        self._stealth = StealthSession(playwright_timeout=60)

    # ------------------------------------------------------------------
    # Fetch helper — retry a few times; see module docstring, this
    # currently always exhausts retries and returns None (AWS WAF captcha
    # block on every layer, does not self-resolve).
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

                start = page * self.PAGE_SIZE
                sep = "&" if "?" in section_url else "?"
                page_url = section_url if start == 0 else f"{section_url}{sep}start={start}"
                html = self._fetch(page_url)
                if html is None:
                    print(f"[{self.site_id}] {category}: page {page} fetch "
                          f"failed (see module docstring: AWS WAF captcha "
                          f"block); next section")
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
                        paper = self._build_paper(it, category)
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

                if len(items) < self.PAGE_SIZE:
                    break
                page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Listing parsing — results link directly to the PDF asset, no
    # separate detail page (see module docstring).
    # ------------------------------------------------------------------

    def _parse_listing(self, soup):
        items = []
        results = soup.select_one("#SearchResults") or soup
        for article in results.select("article"):
            a = article.select_one("a[href]")
            if a is None:
                continue
            href = (a.get("href") or "").strip()
            if not href:
                continue
            url = urljoin(self.base_url, href)

            header = article.select_one(".searchResultHeader") or a.select_one("header")
            title = _clean(header) if header is not None else _clean(a)
            # Strip the trailing "[ PDF , 235 KB]" file-type annotation.
            title = re.sub(r"\s*\[\s*[A-Za-z]+\s*,\s*[\d.,]+\s*[KMG]?B\s*\]\s*$", "", title).strip()
            if not title:
                continue

            abstract_el = article.select_one("p")
            abstract = _clean(abstract_el) if abstract_el else ""

            items.append({"url": url, "title": title, "abstract": abstract})
        return items

    # ------------------------------------------------------------------
    # Build paper dict directly from the listing item (no detail page)
    # ------------------------------------------------------------------

    def _build_paper(self, item, category):
        title = item["title"]
        if not title:
            return None
        abstract = item.get("abstract") or ""
        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            abstract = title

        is_pdf = item["url"].split("?", 1)[0].lower().endswith(".pdf")
        pdf_url = item["url"] if is_pdf else None

        # Try to pull a trailing "- D Month YYYY" / "DD Month YYYY" date out
        # of the title (common in this taxonomy's naming, e.g. "... – 17
        # January 2022"); best-effort only, left blank if absent.
        published_date = ""
        m = re.search(
            r"(\d{1,2}\s+(?:January|February|March|April|May|June|July|"
            r"August|September|October|November|December)\s+\d{4})",
            title,
        )
        if m:
            from datetime import datetime
            try:
                published_date = datetime.strptime(m.group(1), "%d %B %Y").strftime("%Y-%m-%d")
            except ValueError:
                published_date = ""

        slug = item["url"].split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]

        return {
            "site_id": self.site_id,
            "external_id": slug,
            "post_number": slug,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": published_date,
            "url": item["url"],
            "pdf_url": pdf_url,
            "original_filename": None,
            "authors": "New Zealand Ministry of Justice",
            "publisher": "New Zealand Ministry of Justice",
            "journal": "New Zealand Ministry of Justice",
            "category": category,
            "keywords": category,
            "metadata": None,
        }


# ---------------------------------------------------------------------------
# PDF download — justice.govt.nz's ``/assets/Documents/Publications/...``
# asset host is a *different* path than the WAF-blocked
# ``/about/publication-finder/`` and ``/publications/`` (a spot-check 404
# there was a clean 404, not the WAF captcha page), so plain ``requests``
# via the generic ``crawler.pdf_downloader.download_pdf_for`` is used here
# too, mirroring the other two NZ crawlers. In practice this has nothing to
# download until ``crawl()`` can actually save rows (see module docstring —
# currently blocked upstream), but is wired up for when/if that changes.
# ---------------------------------------------------------------------------

def download_pdfs_for_site(conn, site_id=None, limit=None, blob_root=None, delay=1.0):
    """Download pending PDFs for this crawler's documents. Returns (ok, failed)."""
    from crawler import pdf_downloader as _pdfdl

    site_id = site_id or JusticeGovtNzPublicationsCrawler.site_id

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
