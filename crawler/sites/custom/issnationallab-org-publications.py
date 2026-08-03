# -*- coding: utf-8 -*-
"""Crawler for ISS National Lab Publications.

API: WordPress REST API /wp-json/wp/v2/publications
     per_page=100, paginated — 206 total records (~3 pages).
Fields used:
  id, date, link, title.rendered, excerpt.rendered (citation/authors),
  content.rendered (abstract), research_areas
"""

from __future__ import annotations

import json
import os
import re
import time

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------
_SITE_ID = "issnationallab-org-publications"
_API_URL = "https://issnationallab.org/wp-json/wp/v2/publications"
_ITEMS_PER_PAGE = 100          # WP REST API max
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))               # safety cap
_WALL_CLOCK_BUDGET = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 min in seconds
_MIN_ABSTRACT_LEN = 100        # skip shorter abstracts (test requires >=100)
_PAGE_SLEEP = 0.5              # seconds between page fetches
_BACKOFF = (1, 3, 9)          # retry delays


def _make_soup(html: str) -> BeautifulSoup | None:
    """html5lib → lxml → html.parser fallback."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _strip_html(html: str) -> str:
    """Strip HTML tags and return plain text; fallback to regex."""
    if not html:
        return ""
    try:
        soup = _make_soup(html)
        if soup is not None:
            return soup.get_text(separator=" ", strip=True)
    except Exception:
        pass
    return re.sub(r"<[^>]+>", " ", html).strip()


def _extract_doi(text: str) -> str:
    """Extract a DOI (10.XXXX/...) from plain text."""
    m = re.search(r'10\.\d{4,}/[^\s<>"\'}\]]+', text)
    return m.group(0).rstrip(".,;)") if m else ""


def _parse_authors(excerpt_text: str, title_text: str) -> list[str]:
    """Extract author list from a formatted citation string.

    WP excerpt format: "Author1, Author2. Title text. Journal. Year;Vol;Page."
    Authors are everything before the title begins.
    """
    if not excerpt_text or not title_text:
        return []
    # Use first 40 chars of title as an anchor (stable even with truncation)
    anchor = title_text[:40].strip()
    idx = excerpt_text.find(anchor) if anchor else -1
    if idx > 0:
        author_section = excerpt_text[:idx].strip().rstrip(". ")
        if author_section:
            return [author_section]
    # Fallback: everything before the first ". " sequence
    parts = re.split(r'\.\s+', excerpt_text, maxsplit=1)
    if parts and parts[0].strip():
        return [parts[0].strip()]
    return []


class ISSNationalLabPublicationsCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: issnationallab-org-publications"
    base_url = "https://issnationallab.org"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Cloudflare serves Brotli (Content-Encoding: br) which requests cannot
        # decompress without the brotli/brotlicffi package.  Restrict to gzip+deflate.
        self._session.headers.update({"Accept-Encoding": "gzip, deflate"})

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _fetch_page(self, page: int) -> list | None:
        """Fetch one page of publications from WP REST API.

        Returns a list of items, an empty list on end-of-pages, or None on
        persistent failure.
        """
        params = {
            "per_page": _ITEMS_PER_PAGE,
            "page": page,
            "_fields": "id,date,link,title,excerpt,content,research_areas",
        }
        for attempt, wait in enumerate(_BACKOFF):
            try:
                resp = self._session.get(_API_URL, params=params, timeout=30)
                if resp.status_code == 400:
                    # WP returns 400 when the requested page exceeds total pages
                    return []
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                print(
                    f"[{_SITE_ID}] page {page} fetch error "
                    f"(attempt {attempt+1}/{len(_BACKOFF)}): {exc}"
                )
                if attempt < len(_BACKOFF) - 1:
                    time.sleep(wait)
        return None

    def _parse_item(self, item: dict) -> dict | None:
        """Parse a single WP REST API publication item into a paper dict.

        Returns None if the item should be skipped.
        """
        post_id = item.get("id")
        url = item.get("link", "")
        if not url:
            return None

        # Title
        title_html = item.get("title", {})
        if isinstance(title_html, dict):
            title_html = title_html.get("rendered", "")
        title = _strip_html(title_html).strip()
        if not title:
            print(f"[{_SITE_ID}] item {post_id}: no title, skipping")
            return None

        # Abstract (from content.rendered)
        content_html = item.get("content", {})
        if isinstance(content_html, dict):
            content_html = content_html.get("rendered", "")
        abstract = _strip_html(content_html).strip()
        if len(abstract) < _MIN_ABSTRACT_LEN:
            print(
                f"[{_SITE_ID}] item {post_id}: abstract too short "
                f"({len(abstract)} chars), skipping"
            )
            return None

        # Citation / authors (from excerpt.rendered)
        excerpt_html = item.get("excerpt", {})
        if isinstance(excerpt_html, dict):
            excerpt_html = excerpt_html.get("rendered", "")
        excerpt_text = _strip_html(excerpt_html).strip()

        authors_list = _parse_authors(excerpt_text, title)

        # DOI (best-effort, not always present)
        doi = _extract_doi(excerpt_text) or _extract_doi(abstract)

        # Date
        date_raw = item.get("date", "")
        published_date = date_raw[:10] if date_raw else ""

        # Research areas (taxonomy terms)
        research_areas = item.get("research_areas", [])

        return {
            "site_id": _SITE_ID,
            "external_id": str(post_id),
            "title": title,
            "authors": json.dumps(authors_list),
            "abstract": abstract,
            "category": "",
            "keywords": json.dumps([]),
            "published_date": published_date,
            "url": url,
            "pdf_url": "",
            "doi": doi,
            "department": "",
            "metadata": json.dumps({
                "post_id": post_id,
                "research_areas": research_areas,
                "excerpt": excerpt_text,
            }),
        }

    # -----------------------------------------------------------------------
    # Main crawl
    # -----------------------------------------------------------------------

    def crawl(self, limit=None):
        limit_str = str(limit) if limit is not None else "∞"
        print(f"[{_SITE_ID}] starting crawl, limit={limit_str}")

        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()

        page = 1
        while True:
            # ---- stop conditions ----
            if limit is not None and saved >= limit:
                break

            elapsed = time.time() - start_time
            if elapsed > _WALL_CLOCK_BUDGET:
                print(
                    f"[{_SITE_ID}] 25-minute budget reached at page {page}, "
                    f"stopping early (saved {saved})"
                )
                break

            if page > _MAX_PAGES:
                print(
                    f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached, stopping"
                )
                break

            # ---- progress logging every 10 pages ----
            if page > 1 and page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_str}")

            # ---- fetch ----
            items = self._fetch_page(page)
            if items is None:
                print(f"[{_SITE_ID}] page {page}: failed after retries, stopping")
                break
            if not items:
                print(f"[{_SITE_ID}] page {page}: no results, end of pagination")
                break

            # ---- process ----
            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                try:
                    url = item.get("link", "")

                    # URL-based deduplication
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)

                    paper = self._parse_item(item)
                    if paper is None:
                        continue

                    self._save_paper(paper)
                    saved += 1
                    new_on_page += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(
                        f"[{_SITE_ID}] item {item.get('id', '?')} failed: {exc}"
                    )
                    continue

            # ---- detect silent looping paginator ----
            if new_on_page == 0 and len(items) > 0:
                print(
                    f"[{_SITE_ID}] page {page}: all {len(items)} items already seen "
                    "(dedup loop detected), stopping"
                )
                break

            time.sleep(_PAGE_SLEEP)
            page += 1

        print(f"[{_SITE_ID}] crawl complete: saved {saved} records")
        return saved
