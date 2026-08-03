# -*- coding: utf-8 -*-
"""Crawler for KIT Royal Tropical Institute — Research Article publications.

Source: WordPress REST API
  https://www.kit.nl/wp-json/wp/v2/institute_pub?kit_publication_type=34
Taxonomy ID 34 = "Research article" in kit_publication_type.
"""

from __future__ import annotations

import json
import os
import re
import time
from html import unescape
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_SITE_ID = "kit-nl-institute"
_BASE_URL = "https://www.kit.nl"
_API_BASE = "https://www.kit.nl/wp-json/wp/v2/institute_pub"
_PUB_TYPE_RESEARCH_ARTICLE = 34   # kit_publication_type taxonomy ID for "Research article"
_PER_PAGE = 100
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_ABSTRACT_MIN_CHARS = 50     # skip items with shorter abstracts

_MONTHS = {
    # English
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
    # Dutch (KIT is bilingual)
    "januari": "01", "februari": "02", "maart": "03",
    "mei": "05", "juni": "06", "juli": "07", "augustus": "08",
    "oktober": "10",
}


class KitNlInstituteCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: kit-nl-institute"
    base_url = _BASE_URL

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _fetch_api_page(self, page: int):
        """Fetch one page from the WP REST API.

        Returns (items_list, total_pages) on success, None on failure.
        WP returns HTTP 400 when page > total_pages — treated as empty.
        """
        waits = [1, 3, 9]
        last_exc = None
        for attempt, wait in enumerate(waits, start=1):
            try:
                resp = self._session.get(
                    _API_BASE,
                    params={
                        "kit_publication_type": _PUB_TYPE_RESEARCH_ARTICLE,
                        "per_page": _PER_PAGE,
                        "page": page,
                    },
                    timeout=30,
                )
                if resp.status_code == 400:
                    # WordPress returns 400 for out-of-range page numbers
                    return [], 0
                resp.raise_for_status()
                total_pages = int(resp.headers.get("X-WP-TotalPages", 1))
                return resp.json(), total_pages
            except Exception as exc:
                last_exc = exc
                print(
                    f"[{_SITE_ID}] API page {page} attempt {attempt}/{len(waits)} "
                    f"failed: {exc}"
                )
                if attempt < len(waits):
                    time.sleep(wait)
        print(f"[{_SITE_ID}] API page {page} failed after 3 attempts: {last_exc}")
        return None

    # ------------------------------------------------------------------
    # HTML parsing helpers
    # ------------------------------------------------------------------

    def _parse_html(self, html: str):
        """Try html5lib → lxml → html.parser fallback chain."""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception as exc:
                print(f"[{_SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
        return None

    @staticmethod
    def _clean(text: str) -> str:
        text = unescape(str(text))
        text = text.replace("\xa0", " ").replace("​", "")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _extract_abstract(self, content_html: str) -> str:
        """Extract abstract paragraphs from content.rendered.

        The WP content contains tabbed metadata (authors, year, SDGs) followed
        by the main abstract text in <p> tags.  We collect <p> tags until we
        hit a download or related-publication section.
        """
        soup = self._parse_html(content_html)
        if soup is None:
            # Fallback: strip all tags and grab first 2000 chars
            flat = re.sub(r"<[^>]+>", " ", content_html)
            flat = re.sub(r"\s+", " ", flat).strip()
            return flat[:2000]

        paragraphs = []
        for p in soup.find_all("p"):
            text = self._clean(p.get_text(" ", strip=True))
            if len(text) < 30:
                continue
            # Stop at download/related-publication markers
            if re.search(r"\[pdf\]|Institute Publication", text, re.I):
                break
            paragraphs.append(text)
            if len(paragraphs) >= 5:
                break

        return "\n\n".join(paragraphs)

    def _extract_authors(self, content_html: str) -> str:
        """Extract author names from /institute/staff/ links, or from text fallback."""
        soup = self._parse_html(content_html)
        if soup is None:
            return ""

        authors = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            if "/institute/staff/" in a.get("href", ""):
                name = self._clean(a.get_text(strip=True))
                if name and name not in seen:
                    authors.append(name)
                    seen.add(name)

        if not authors:
            # Fallback: "Authors [names] Publication year" text pattern
            flat = re.sub(r"<[^>]+>", " ", content_html)
            flat = re.sub(r"\s+", " ", flat)
            m = re.search(r"Authors\s+([^P]{5,200}?)\s+Publication year", flat)
            if m:
                for name in re.split(r",\s*", m.group(1).strip()):
                    name = self._clean(name)
                    if name and name not in seen:
                        authors.append(name)
                        seen.add(name)

        return "; ".join(authors)

    def _extract_pdf_url(self, content_html: str) -> str:
        """Return the first PDF from wp-content/uploads."""
        matches = re.findall(
            r'href="(https://www\.kit\.nl/wp-content/uploads/[^"]+\.pdf)"',
            content_html,
        )
        return matches[0] if matches else ""

    def _extract_pub_date(self, content_html: str, fallback_date: str) -> str:
        """Extract publication date from 'Publication year [Month] [Year]' in content."""
        flat = re.sub(r"<[^>]+>", " ", content_html)
        flat = re.sub(r"\s+", " ", flat)

        m = re.search(r"Publication year\s+([A-Za-z]+)\s+(\d{4})", flat)
        if m:
            month_str = m.group(1).lower()
            year = m.group(2)
            month = _MONTHS.get(month_str, "01")
            return f"{year}-{month}-01"

        m = re.search(r"Publication year\s+(\d{4})", flat)
        if m:
            return f"{m.group(1)}-01-01"

        # Fallback: use WordPress post date
        dm = re.match(r"(\d{4}-\d{2}-\d{2})", fallback_date or "")
        return dm.group(1) if dm else ""

    # ------------------------------------------------------------------
    # Item parser
    # ------------------------------------------------------------------

    def _parse_item(self, item: dict) -> dict | None:
        content_html = item.get("content", {}).get("rendered", "")
        date_str = item.get("date", "")

        title = self._clean(item.get("title", {}).get("rendered", ""))
        if not title:
            return None

        url = item.get("link", "")
        if not url:
            return None

        external_id = str(item.get("id", ""))
        slug = item.get("slug", "")

        abstract = self._extract_abstract(content_html)
        authors = self._extract_authors(content_html)
        pdf_url = self._extract_pdf_url(content_html)
        published_date = self._extract_pub_date(content_html, date_str)

        # listed_date: when WordPress published the post
        dm = re.match(r"(\d{4}-\d{2}-\d{2})", date_str)
        listed_date = dm.group(1) if dm else ""

        # original_filename from PDF URL path
        original_filename = ""
        if pdf_url:
            original_filename = urlparse(pdf_url).path.rsplit("/", 1)[-1]

        metadata = {
            "post_id": item.get("id"),
            "slug": slug,
            "posted_date": listed_date,
            "date_raw": date_str,
            "kit_publication_type": item.get("kit_publication_type"),
            "kit_impact_area": item.get("kit_impact_area"),
            "kit_topic": item.get("kit_topic"),
            "kit_sdg": item.get("kit_sdg"),
        }

        return {
            "site_id": _SITE_ID,
            "external_id": external_id,
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "publisher": "KIT Royal Tropical Institute",
            "published_date": published_date,
            "posted_date": listed_date,
            "url": url,
            "pdf_url": pdf_url,
            "keywords": "Research article",
            "category": "Research article",
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        saved = 0
        seen_urls: set[str] = set()
        limit_label = str(limit) if limit is not None else "∞"
        crawl_start = time.time()

        page = 1
        total_pages = None

        while True:
            # Wall-clock budget: 25 minutes
            if time.time() - crawl_start > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached; stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page > _MAX_PAGES:
                print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached; stopping.")
                break

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_label}")

            try:
                result = self._fetch_api_page(page)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] failed to fetch API page {page}: {exc}")
                break

            if result is None:
                print(f"[{_SITE_ID}] API fetch returned None at page {page}; stopping.")
                break

            items, tp = result

            if total_pages is None:
                total_pages = tp

            if not items:
                print(f"[{_SITE_ID}] no items on page {page}; stopping.")
                break

            # Detect pagination loop: all URLs already seen → stop
            new_urls = [it.get("link") for it in items if it.get("link") not in seen_urls]
            if not new_urls and seen_urls:
                print(f"[{_SITE_ID}] all items on page {page} already seen; stopping.")
                break

            if page == _MAX_PAGES:
                print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached; stopping.")

            for item in items:
                if limit is not None and saved >= limit:
                    break

                url = item.get("link", "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)

                try:
                    time.sleep(self._delay)
                    paper = self._parse_item(item)
                    if paper is None:
                        print(f"[{_SITE_ID}] item {url}: parse returned None; skipping")
                        continue

                    abstract = paper.get("abstract", "")
                    if len(abstract) < _ABSTRACT_MIN_CHARS:
                        print(
                            f"[{_SITE_ID}] item {url} skipped: abstract too short "
                            f"({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {url} failed: {exc}")
                    continue

            if total_pages is not None and page >= total_pages:
                break

            page += 1

        print(f"[{_SITE_ID}] crawl complete: saved {saved}/{limit_label}")
        return saved
