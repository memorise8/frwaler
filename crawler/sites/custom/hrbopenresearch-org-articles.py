# -*- coding: utf-8 -*-
"""HRB Open Research (hrbopenresearch.org) — F1000-platform article crawler.

Site is behind Cloudflare (plain requests/curl_cffi → 403 "Just a moment...").
Every fetch goes through ``crawler.stealth_fetcher.StealthSession``, whose
playwright fallback layer passes the JS challenge. Fetches are slow (several
seconds each, sometimes needing a retry when Cloudflare re-challenges), so
this crawler budgets wall-clock time and keeps a page safety cap.

Listing:  https://hrbopenresearch.org/browse/articles (paginates via ?page=N)
Article:  https://hrbopenresearch.org/articles/{volume}-{id}

Article detail pages carry standard Highwire/PRISM ``<meta>`` citation tags
(``citation_title``, ``citation_abstract``, ``citation_author`` (repeated),
``citation_publication_date``, ``citation_pdf_url``, ``citation_doi``, ...)
which is a far more reliable extraction surface than scraping visible HTML.
Note: text in these meta tags is double-HTML-escaped in the source (e.g.
``&amp;ldquo;``) and needs unescaping twice.
"""

import html as html_module
import json
import os
import re
import sys
import time
from pathlib import Path

# Absolute import: spec_from_file_location has no package context.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from crawler.base_crawler import BaseCrawler  # noqa: E402
from crawler.stealth_fetcher import StealthSession  # noqa: E402


def _clean_text(text) -> str:
    """Unescape double-encoded HTML entities and collapse whitespace."""
    if not text:
        return ""
    text = html_module.unescape(html_module.unescape(str(text)))
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(raw) -> str:
    """Normalise 'YYYY/MM/DD' or 'YYYY-MM-DD...' → 'YYYY-MM-DD'."""
    if not raw:
        return ""
    raw = str(raw).strip()
    m = re.match(r"^(\d{4})/(\d{2})/(\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    if re.match(r"^\d{4}-\d{2}-\d{2}", raw):
        return raw[:10]
    return raw


class HrbOpenResearchArticlesCrawler(BaseCrawler):
    """Crawls HRB Open Research's article listing + detail pages."""

    site_id = "hrbopenresearch-org"
    site_name = "HRB Open Research"
    base_url = "https://hrbopenresearch.org"

    _BROWSE_URL = "https://hrbopenresearch.org/browse/articles"
    _ARTICLE_RE = re.compile(
        r'href="(https://hrbopenresearch\.org/articles/(\d+)-(\d+))"'
    )
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "60"))
    _BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _FETCH_RETRIES = 5

    def __init__(self, db_conn, delay=1.5):
        super().__init__(db_conn, delay=delay)
        self._stealth = StealthSession(playwright_timeout=60)

    # ------------------------------------------------------------------
    # Fetch helper — Cloudflare occasionally re-challenges, retry a few times
    # ------------------------------------------------------------------

    def _fetch(self, url):
        for attempt in range(self._FETCH_RETRIES):
            html, info = self._stealth.fetch_html(url)
            if html and info.get("final_reason") == "ok":
                return html
            print(f"[{self.site_id}] fetch attempt {attempt + 1}/"
                  f"{self._FETCH_RETRIES} failed for {url}: "
                  f"{info.get('final_reason')}")
            if attempt < self._FETCH_RETRIES - 1:
                time.sleep(3 * (attempt + 1))
        return None

    # ------------------------------------------------------------------
    # Crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        page = 1

        while True:
            if time.time() - start_time > self._BUDGET_SECONDS:
                print(f"[{self.site_id}] 25-min budget reached at page {page}. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page > self._MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")
                break

            page_url = self._BROWSE_URL if page == 1 else f"{self._BROWSE_URL}?page={page}"
            html = self._fetch(page_url)
            if html is None:
                print(f"[{self.site_id}] Failed to fetch listing page {page}. Stopping.")
                break

            new_links = []
            for m in self._ARTICLE_RE.finditer(html):
                article_url = m.group(1)
                if article_url not in seen_urls:
                    seen_urls.add(article_url)
                    new_links.append((article_url, m.group(2), m.group(3)))

            if not new_links:
                print(f"[{self.site_id}] No new article links at page {page}. Done.")
                break

            for article_url, volume, article_id in new_links:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > self._BUDGET_SECONDS:
                    print(f"[{self.site_id}] 25-min budget reached mid-page {page}. Stopping.")
                    break

                try:
                    time.sleep(self._delay)
                    paper = self._scrape_article(article_url, volume, article_id)
                    if paper is None:
                        continue
                    self._save_paper(paper)
                    saved += 1
                    limit_disp = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] Saved {saved}/{limit_disp}: {paper['title'][:60]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] article {article_url} failed: {exc}")
                    continue

            print(f"[{self.site_id}] page {page}: saved {saved} so far")
            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Article detail scraping
    # ------------------------------------------------------------------

    def _scrape_article(self, url, volume, article_id):
        external_id = f"{volume}-{article_id}"

        html = self._fetch(url)
        if html is None:
            print(f"[{self.site_id}] Skipping {external_id}: fetch failed")
            return None

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")

        def _meta(name):
            tag = soup.find("meta", attrs={"name": name})
            return tag.get("content") if tag else None

        def _metas(name):
            return [
                t.get("content") for t in soup.find_all("meta", attrs={"name": name})
                if t.get("content")
            ]

        title = _clean_text(_meta("citation_title") or _meta("dc.title"))
        if not title and soup.title and soup.title.string:
            title = _clean_text(soup.title.string).split(" | ")[0]

        abstract = _clean_text(_meta("citation_abstract") or _meta("dc.description"))
        if len(abstract) < 30:
            print(f"[{self.site_id}] Skipping {external_id}: abstract too short "
                  f"({len(abstract)} chars)")
            return None

        authors_list = [a.strip() for a in _metas("citation_author") if a and a.strip()]
        authors = "; ".join(authors_list) if authors_list else None

        pub_date_raw = _meta("citation_publication_date") or _meta("dc.date") or ""
        published_date = _parse_date(pub_date_raw)

        pdf_url = _meta("citation_pdf_url") or f"{url}/pdf"
        doi = _meta("citation_doi")
        keywords = _clean_text(_meta("citation_keywords") or "")
        version = _meta("citation_version_number")

        original_filename = f"hrbopenresearch-org_{external_id}.pdf"

        metadata_dict = {
            "doi": doi,
            "volume": volume,
            "article_id": article_id,
            "version": version,
            "source_url": url,
        }

        return {
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": external_id,
            "title": title or "(untitled)",
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": published_date,
            "url": url,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "authors": authors,
            "publisher": "HRB Open Research",
            "journal": "HRB Open Research",
            "category": None,
            "keywords": keywords or None,
            "metadata": json.dumps(metadata_dict, ensure_ascii=False),
        }
