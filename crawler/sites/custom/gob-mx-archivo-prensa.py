# -*- coding: utf-8 -*-
"""gob.mx Archivo de Prensa crawler — HTML scrape via StealthSession.

Base crawler for ``https://www.gob.mx/{ministry}/archivo/prensa`` press
release archives, parametrized over ministry. ``www.gob.mx`` blocks plain
``requests``/curl with a Cloudflare-style "Challenge Validation" page, so
every fetch goes through :class:`crawler.stealth_fetcher.StealthSession`
(its ``curl_cffi`` layer passes the challenge).

This module intentionally does NOT expose a concrete ``site_id`` — it is
left as ``None`` so the auto-discovery loop in
``crawler/sites/__init__.py`` (which registers any class with a
``site_id``/``crawl`` pair) skips it. The real, instantiable crawlers are
per-ministry subclasses built at runtime by
``scripts/collect_gob_mx_prensa.py`` via ``type(...)``, each overriding
``site_id`` / ``site_name`` / ``base_url`` / ``MINISTRY_SLUG`` /
``MINISTRY_LABEL``.

Listing pages paginate via ``?idiom=es&page=N`` (N starts at 1), yielding
~9 article links per page (``/{ministry}/prensa/<slug>``). Pagination
stops when a page returns no NEW article links (dedup by URL) or an empty
page. Article detail pages carry the title in ``<h1>``, the body text in
CSS selector ``.article-body`` (used as the abstract), and the article's
own publication date in an inline ``properties.published_on = "YYYY-MM-DD
..."`` JS assignment (verified against several ministries/articles — this
is far more reliable than scanning the page text for the first
``YYYY-MM-DD`` token, which usually matches a "related articles" sidebar
date instead of the article's own date). These press releases are plain
HTML — there is no PDF, so ``pdf_url`` is always ``None``.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from bs4 import BeautifulSoup  # noqa: E402

from crawler.base_crawler import BaseCrawler  # noqa: E402
from crawler.stealth_fetcher import StealthSession  # noqa: E402


# The article's own date lives in this inline JS assignment on the detail
# page — NOT the first YYYY-MM-DD token in the page (that one usually
# belongs to a "related articles" sidebar entry).
_PUBLISHED_ON_RE = re.compile(r'properties\.published_on\s*=\s*"(\d{4}-\d{2}-\d{2})')
_DATE_FALLBACK_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class GobMxArchivoPrensaCrawler(BaseCrawler):
    """Abstract base — scrapes ``https://www.gob.mx/{ministry}/archivo/prensa``.

    Not auto-registered (``site_id = None``). Concrete per-ministry
    subclasses are created by the runner script; see module docstring.
    """

    site_id = None
    site_name = None
    base_url = None

    # Overridden per-ministry by the runner.
    MINISTRY_SLUG: str | None = None
    MINISTRY_LABEL: str | None = None

    WALL_CLOCK_BUDGET_SEC = 20 * 60
    PAGE_SAFETY_CAP = 200
    MIN_ABSTRACT_CHARS = 100

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn, delay=delay)
        self._stealth = StealthSession()

    def crawl(self, limit=None):
        if not self.MINISTRY_SLUG:
            raise RuntimeError(f"{type(self).__name__}: MINISTRY_SLUG not set")

        slug = self.MINISTRY_SLUG
        label = self.MINISTRY_LABEL or slug
        link_re = re.compile(rf'href="(/{re.escape(slug)}/prensa/[^"]+)"')

        saved = 0
        page = 1
        seen_urls: set = set()
        deadline = time.monotonic() + self.WALL_CLOCK_BUDGET_SEC
        limit_label = str(limit) if limit is not None else "inf"

        while True:
            if limit is not None and saved >= limit:
                break
            if page > self.PAGE_SAFETY_CAP:
                print(f"[{self.site_id}] safety cap ({self.PAGE_SAFETY_CAP}) "
                      f"reached at page {page}; stopping")
                break
            if time.monotonic() > deadline:
                print(f"[{self.site_id}] wall-clock budget exhausted; "
                      f"stopping at page {page}")
                break

            list_url = f"https://www.gob.mx/{slug}/archivo/prensa?idiom=es&page={page}"
            time.sleep(self._delay)
            html, reason = self._stealth.fetch_html(list_url)
            if not html:
                print(f"[{self.site_id}] page {page} fetch failed "
                      f"({reason.get('final_reason')}); stopping")
                break

            page_urls = []
            for href in link_re.findall(html):
                url = "https://www.gob.mx" + href
                if url not in page_urls:
                    page_urls.append(url)

            if not page_urls:
                print(f"[{self.site_id}] page {page}: no article links; stopping")
                break

            new_in_page = 0
            for article_url in page_urls:
                if limit is not None and saved >= limit:
                    break
                if article_url in seen_urls:
                    continue
                seen_urls.add(article_url)
                new_in_page += 1

                try:
                    paper = self._scrape_article(article_url, label)
                    if paper is None:
                        continue
                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit_label}"
                    print(f"[{self.site_id}] saved {counter}: {paper['title'][:80]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] article failed ({article_url}): {exc}")
                    continue

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            if new_in_page == 0:
                print(f"[{self.site_id}] page {page}: no new article links "
                      f"(all duplicates); stopping")
                break

            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Article scraping
    # ------------------------------------------------------------------

    def _scrape_article(self, article_url, label):
        time.sleep(self._delay)
        html, reason = self._stealth.fetch_html(article_url)
        if not html:
            print(f"[{self.site_id}] article fetch failed ({article_url}): "
                  f"{reason.get('final_reason')}")
            return None

        soup = BeautifulSoup(html, "html.parser")

        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else None
        if not title:
            print(f"[{self.site_id}] article skipped (no title): {article_url}")
            return None

        body_el = soup.select_one(".article-body")
        abstract = body_el.get_text(" ", strip=True) if body_el else ""
        abstract = re.sub(r"\s+", " ", abstract).strip()
        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            print(f"[{self.site_id}] article skipped (abstract too short, "
                  f"{len(abstract)} chars): {article_url}")
            return None

        published_date = self._extract_date(html)

        slug_path = article_url.split("?", 1)[0].rstrip("/")
        external_id = slug_path.rsplit("/", 1)[-1]

        return {
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": external_id,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": published_date,
            "url": article_url,
            "pdf_url": None,
            "original_filename": None,
            "authors": label,
            "publisher": label,
            "category": None,
            "keywords": None,
            "metadata": None,
        }

    @staticmethod
    def _extract_date(html):
        m = _PUBLISHED_ON_RE.search(html)
        if m:
            return m.group(1)
        # Best-effort fallback if the JS variable ever disappears.
        m2 = _DATE_FALLBACK_RE.search(html)
        return m2.group(0) if m2 else None
