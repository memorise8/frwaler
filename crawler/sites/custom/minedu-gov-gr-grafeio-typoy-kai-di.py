# -*- coding: utf-8 -*-
"""Crawler for Greek Ministry of Education Press Releases.

Target: https://www.minedu.gov.gr/grafeio-typoy-kai-dimosion-sxeseon/deltia-typoy
Site: Joomla-based, HTML listing + detail pages, 20 items/page.
Article IDs: numeric Joomla ID embedded in URL slug (e.g. /rss/64902-...).
Date: embedded in title prefix as DD-MM-YY.

Repaired 2026-08-05: the Joomla template was redesigned. The listing no
longer renders a `<table class="category">`; it now renders
`<div class="custom-article-list"><li class="list-group-item...">
<article class="custom-article-item">` items, with the title link in
`h4.custom-article-title a`. Page size is now 20 (was 10); `?start=N`
pagination still works unlinked (confirmed working query param even though
no pagination nav is rendered server-side). Also: article hrefs on page 1
use the non-SEO `/?view=article&id=NNNNN:slug&catid=...` form, which 301-
redirects to the canonical `/site/NNNNN-slug` detail URL — the crawler's
curl call was missing `-L`, so those redirects returned an empty stub body
instead of the article HTML (-> "No item-page div" skip for every post
using that href form). Added `-L` and made the Joomla-ID regex fall back to
the `id=(\\d+)` query form when the URL has no `/(\\d+)-` slug segment.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, __import__("os").path.dirname(
    __import__("os").path.dirname(
        __import__("os").path.dirname(
            __import__("os").path.abspath(__file__)
        )
    )
))

from crawler.base_crawler import BaseCrawler  # noqa: E402

_SITE_ID = "minedu-gov-gr-grafeio-typoy-kai-di"
_BASE_URL = "https://www.minedu.gov.gr"
_LIST_URL = f"{_BASE_URL}/grafeio-typoy-kai-dimosion-sxeseon/deltia-typoy"
_PAGE_SIZE = 20
_SAFETY_PAGE_CAP = 200
_PUBLISHER = "Υπουργείο Παιδείας, Θρησκευμάτων & Αθλητισμού"
_DEPARTMENT = "Γραφείο Τύπου και Δημοσίων Σχέσεων"


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except ImportError:
            continue
        except Exception:
            continue
    raise RuntimeError("No HTML parser available")


def _curl_get(url: str, retries: int = 3):
    """Fetch URL via curl with exponential backoff. Returns decoded text or None."""
    cmd = [
        "curl", "--tls-max", "1.3", "-skL", "--max-time", "30",
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: el,en;q=0.5",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            if result.stdout and result.stdout.strip():
                try:
                    return result.stdout.decode("utf-8")
                except UnicodeDecodeError:
                    return result.stdout.decode("utf-8", errors="replace")
            if attempt < retries - 1:
                wait = (attempt + 1) ** 2
                print(f"[{_SITE_ID}] Empty response (attempt {attempt + 1}/{retries}), retry in {wait}s")
                time.sleep(wait)
        except subprocess.TimeoutExpired:
            if attempt < retries - 1:
                wait = (attempt + 1) ** 2
                print(f"[{_SITE_ID}] Timeout (attempt {attempt + 1}/{retries}), retry in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = (attempt + 1) ** 2
                print(f"[{_SITE_ID}] curl error (attempt {attempt + 1}/{retries}): {exc}, retry in {wait}s")
                time.sleep(wait)
            else:
                print(f"[{_SITE_ID}] curl failed after {retries} attempts for {url}: {exc}")
    return None


def _parse_date_from_title(title: str) -> str:
    """Parse leading DD-MM-YY from title → 'YYYY-MM-DD', or '' if not found."""
    m = re.match(r'^(\d{2})-(\d{2})-(\d{2})\b', title.strip())
    if not m:
        return ""
    day, month, year_short = int(m.group(1)), int(m.group(2)), int(m.group(3))
    year = 2000 + year_short
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _extract_joomla_id(url: str):
    """Extract numeric Joomla article ID from URL like /rss/64902-slug, or
    from the non-SEO `?view=article&id=64902:slug` form."""
    m = re.search(r'/(\d+)-', url)
    if m:
        return m.group(1)
    m = re.search(r'[?&]id=(\d+)', url)
    return m.group(1) if m else None


def _find_pdf(soup) -> tuple:
    """Return (pdf_url, original_filename) from page, or (None, None)."""
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".pdf" in href.lower():
            if not href.startswith("http"):
                href = _BASE_URL + href
            filename = href.rstrip("/").split("/")[-1].split("?")[0] or None
            return href, filename
    return None, None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class MineduGovGrGrafeioTypoyKaiDiCrawler(BaseCrawler):
    """Greek Ministry of Education – Press Office press releases."""

    site_id = _SITE_ID
    site_name = "Custom: minedu-gov-gr-grafeio-typoy-kai-di"
    base_url = _BASE_URL

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        start_offset = 0
        page = 1
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        while True:
            # Wall-clock budget
            if time.time() - start_time > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[{_SITE_ID}] 25-minute budget reached at page {page}. Exiting cleanly.")
                break

            if limit is not None and saved >= limit:
                break

            if page > _SAFETY_PAGE_CAP:
                print(f"[{_SITE_ID}] Safety cap of {_SAFETY_PAGE_CAP} pages reached. Stopping.")
                break

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_str}")

            # Fetch listing page
            list_url = f"{_LIST_URL}?start={start_offset}" if start_offset > 0 else _LIST_URL
            raw = _curl_get(list_url)
            if not raw:
                print(f"[{_SITE_ID}] Failed to fetch listing page {page} (start={start_offset}). Stopping.")
                break

            try:
                soup = _make_soup(raw)
            except Exception as exc:
                print(f"[{_SITE_ID}] Failed to parse listing page {page}: {exc}. Stopping.")
                break

            container = soup.find("div", class_="custom-article-list") or soup.find(
                "div", class_="custom-category-listing"
            )
            if not container:
                print(f"[{_SITE_ID}] No article list container at page {page}. Stopping.")
                break

            articles = []
            for article_el in container.find_all("article", class_="custom-article-item"):
                title_el = article_el.find("h4", class_="custom-article-title")
                a_tag = title_el.find("a", href=True) if title_el else None
                if not a_tag:
                    continue
                title = a_tag.get_text(strip=True)
                href = a_tag["href"]
                if not href.startswith("http"):
                    href = _BASE_URL + href
                articles.append({"title": title, "url": href})

            if not articles:
                print(f"[{_SITE_ID}] No articles at page {page} (start={start_offset}). Done.")
                break

            # Detect pagination loop
            new_articles = [a for a in articles if a["url"] not in seen_urls]
            if not new_articles and page > 1:
                print(f"[{_SITE_ID}] All articles on page {page} already seen. Stopping.")
                break

            for article_meta in articles:
                if limit is not None and saved >= limit:
                    break

                url = article_meta["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                try:
                    n = self._process_article(article_meta)
                    saved += n
                    if n:
                        print(f"[{_SITE_ID}] Saved {saved}/{limit_str}: {article_meta['title'][:60]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {url} failed: {exc}")
                    continue

                time.sleep(self._delay)

            start_offset += _PAGE_SIZE
            page += 1

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved

    def _process_article(self, meta: dict) -> int:
        """Fetch and save one article. Returns 1 if saved, 0 if skipped."""
        url = meta["url"]
        title = meta["title"]

        joomla_id = _extract_joomla_id(url)
        published_date = _parse_date_from_title(title)

        raw = _curl_get(url)
        if not raw:
            print(f"[{_SITE_ID}] Failed to fetch detail: {url}")
            return 0

        try:
            soup = _make_soup(raw)
        except Exception as exc:
            print(f"[{_SITE_ID}] Parse error for {url}: {exc}")
            return 0

        # Joomla now renders the class as a single unspaced token per
        # category, e.g. class="... item-pageDeltia-Typou" instead of a
        # separate "item-page" class, so match by prefix instead of exact
        # class equality.
        content_div = None
        for div in soup.find_all("div", class_=True):
            if any(c.startswith("item-page") for c in div.get("class", [])):
                content_div = div
                break
        if not content_div:
            print(f"[{_SITE_ID}] No item-page div: {url}")
            return 0

        abstract = content_div.get_text(" ", strip=True)

        # Strip repeated title from the start
        if abstract.startswith(title):
            abstract = abstract[len(title):].strip()
        # Also strip a shorter title variant that may appear (heading)
        for prefix in re.findall(r'^[^\n]{10,120}', abstract):
            clean = re.sub(r'\s+', ' ', prefix).strip()
            if clean == re.sub(r'\s+', ' ', title).strip():
                abstract = abstract[len(prefix):].strip()
                break

        abstract = re.sub(r'\s+', ' ', abstract).strip()

        if len(abstract) < 50:
            print(f"[{_SITE_ID}] Skipping (abstract too short, {len(abstract)} chars): {title[:60]}")
            return 0

        pdf_url, original_filename = _find_pdf(soup)

        paper = {
            "site_id": _SITE_ID,
            "external_id": joomla_id,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": published_date,
            "url": url,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "authors": None,
            "publisher": _PUBLISHER,
            "department": _DEPARTMENT,
            "journal": None,
            "category": "Δελτία Τύπου",
            "keywords": None,
            "doi": None,
            "metadata": json.dumps({
                "posted_date": published_date,
                "joomla_id": joomla_id,
                "category": "Δελτία Τύπου",
                "originalFilename": original_filename,
            }, ensure_ascii=False),
        }

        self._save_paper(paper)
        return 1
