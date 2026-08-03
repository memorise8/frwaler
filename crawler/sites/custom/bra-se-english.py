# -*- coding: utf-8 -*-
"""Crawler for BRA.se (Swedish National Council for Crime Prevention) English publications.

Starting URL: https://bra.se/english/publications
List API:     SiteVision faceted-search AJAX endpoint — query=the returns ~300 publications.
              Page 1: ?state=ajaxQuery&isRenderingAjaxResult=true&query=the
              Pages 2+: ?state=executePaging&isRenderingAjaxPagingResult=true&query=the&startAtHit=N
              10 items/page; hits: title, abstract snippet, date, category tags, detail URL.
Detail pages: /english/publications/archive/YYYY-MM-DD-slug
              Used to extract PDF download URL; abstract comes from the list snippet.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from html import unescape
from typing import Optional

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE_URL = "https://bra.se"

# SiteVision faceted-search portlet ID (stable across sessions)
_PORTLET_PATH = "/4.6af85320191e4f4f8b94593/12.6af85320191e4f4f8b945a2.htm"
_QUERY = "the"  # broad English term — matches virtually all EN publications

_LIST_URL_FIRST = (
    f"{_BASE_URL}{_PORTLET_PATH}"
    f"?state=ajaxQuery&isRenderingAjaxResult=true&query={_QUERY}"
)
_LIST_URL_PAGE = (
    f"{_BASE_URL}{_PORTLET_PATH}"
    f"?state=executePaging&isRenderingAjaxPagingResult=true"
    f"&query={_QUERY}&startAtHit={{offset}}"
)

_PAGE_SIZE = 10
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))       # safety cap — log when reached
_MAX_WALL_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes
_MIN_ABSTRACT = 50     # skip items with fewer chars


# ---------------------------------------------------------------------------
# Module-level network helper (no class dependency)
# ---------------------------------------------------------------------------

def _curl(url: str, retries: int = 3) -> str:
    """GET *url* via curl (TLS-1.3, insecure). Returns decoded text or '' on failure."""
    backoff = [1, 3, 9]
    for attempt in range(retries):
        if attempt > 0:
            wait = backoff[attempt - 1]
            print(f"[bra-se-english] retry {attempt}/{retries-1} in {wait}s for {url[:80]}")
            time.sleep(wait)
        try:
            cmd = [
                "curl", "--tls-max", "1.3", "-skL",
                "--max-time", "30",
                "-H", ("User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
                "-H", "Accept: text/html,*/*",
                "-H", "Accept-Language: en-GB,en;q=0.9,sv;q=0.8",
                url,
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            if result.returncode == 0 and result.stdout:
                return result.stdout.decode("utf-8", errors="replace")
            print(f"[bra-se-english] curl exit {result.returncode} for {url[:80]}")
        except subprocess.TimeoutExpired:
            print(f"[bra-se-english] curl timeout (attempt {attempt+1}) for {url[:80]}")
        except Exception as exc:
            print(f"[bra-se-english] curl error (attempt {attempt+1}): {exc}")
    return ""


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

def _strip_tags(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _make_soup(html: str):
    """Return a BeautifulSoup object, trying parsers in order. Returns None if unavailable."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except ImportError:
            continue
        except Exception:
            continue
    return None


def _parse_list_items(html: str) -> list[dict]:
    """Extract publication items from a list-page HTML fragment.

    Each item is a dict with keys: url, title, subtitle, abstract, date, category.
    """
    items: list[dict] = []
    # Strip HTML comments (the page wraps a duplicate text block in a comment)
    html_nc = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)

    # Split on list item boundaries
    hit_blocks = re.split(
        r'(?=<li[^>]+class="[^"]*sv-search-hit[^"]*")',
        html_nc,
    )

    for block in hit_blocks:
        if "sv-search-hit" not in block:
            continue
        if "/english/publications/" not in block:
            continue
        try:
            item = _parse_single_item(block)
            if item:
                items.append(item)
        except Exception as exc:
            print(f"[bra-se-english] item parse error: {exc}")
            continue

    return items


def _parse_single_item(block: str) -> Optional[dict]:
    """Parse one <li class="sv-search-hit"> block into a dict."""
    # URL and title from anchor inside h3.bra-reports__content--title
    link_m = re.search(
        r'<h3[^>]*bra-reports__content--title[^>]*>.*?'
        r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>',
        block, re.DOTALL,
    )
    if not link_m:
        return None

    url_path = link_m.group(1).strip()
    title = _strip_tags(link_m.group(2))

    if not url_path.startswith("/english/publications/"):
        return None
    if not title:
        return None

    # Subtitle from h4.bra-reports__content--sub-title
    sub_m = re.search(
        r'<h4[^>]*bra-reports__content--sub-title[^>]*>(.*?)</h4>',
        block, re.DOTALL,
    )
    subtitle = _strip_tags(sub_m.group(1)) if sub_m else ""

    # Abstract and date from <p class="normal" style="margin-top:0.5em">
    paras = re.findall(
        r'<p[^>]*class="normal"[^>]*style="margin-top:0\.5em"[^>]*>(.*?)</p>',
        block, re.DOTALL,
    )

    abstract_parts: list[str] = []
    date = ""
    for raw_p in paras:
        text = _strip_tags(raw_p)
        if not text:
            continue
        # Swedish "Publicerad YYYY-MM-DD" = published date paragraph
        m = re.match(r"Publicerad\s+(\d{4}-\d{2}-\d{2})", text)
        if m:
            date = m.group(1)
        else:
            abstract_parts.append(text)

    abstract = " ".join(abstract_parts).strip()

    # Fallback: date from URL slug
    if not date:
        slug_m = re.search(r"/archive/(\d{4}-\d{2}-\d{2})", url_path)
        if slug_m:
            date = slug_m.group(1)

    # Augment short abstracts with subtitle
    if len(abstract) < 100 and subtitle:
        abstract = (subtitle + ". " + abstract).strip()

    # Category tags
    tags = re.findall(
        r'<li[^>]*bra-reports__tags--item[^>]*>(.*?)</li>',
        block, re.DOTALL,
    )
    category = ", ".join(
        _strip_tags(t) for t in tags if _strip_tags(t)
    )

    return {
        "url": url_path,
        "title": title,
        "subtitle": subtitle,
        "abstract": abstract,
        "date": date,
        "category": category,
    }


def _get_pdf_url(detail_html: str) -> str:
    """Extract first PDF download URL from a detail page."""
    m = re.search(
        r'href="(https://bra\.se/download/[^"]+\.pdf(?:[^"]*)?)"',
        detail_html,
    )
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class BraSEEnglishCrawler(BaseCrawler):
    """Crawler for BRA.se English publications."""

    site_id = "bra-se-english"
    site_name = "Custom: bra-se-english"
    base_url = _BASE_URL

    def crawl(self, limit=None) -> int:
        """Crawl BRA English publications, saving up to *limit* items (None = all)."""
        seen_urls: set[str] = set()
        saved = 0
        page = 0
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "inf"

        while True:
            # --- Wall clock budget ---
            if time.time() - start_time > _MAX_WALL_SECS:
                print("[bra-se-english] 25-minute wall clock budget exceeded, stopping cleanly.")
                break

            # --- Limit check ---
            if limit is not None and saved >= limit:
                break

            # --- Safety cap ---
            if page >= _MAX_PAGES:
                print(f"[bra-se-english] Safety cap of {_MAX_PAGES} pages reached, stopping.")
                break

            # --- Fetch list page ---
            offset = page * _PAGE_SIZE
            list_url = _LIST_URL_FIRST if page == 0 else _LIST_URL_PAGE.format(offset=offset)

            time.sleep(self._delay)
            raw = _curl(list_url)
            if not raw:
                print(f"[bra-se-english] Failed to fetch list page {page + 1}, skipping.")
                page += 1
                continue

            # --- Parse items ---
            items = _parse_list_items(raw)
            if not items:
                print(f"[bra-se-english] No items on page {page + 1}. Done.")
                break

            # --- Dedup check for infinite-loop protection ---
            new_items = [it for it in items if it["url"] not in seen_urls]
            if not new_items:
                print(f"[bra-se-english] All items on page {page + 1} already seen. Done.")
                break

            # --- Process each item ---
            for it in new_items:
                if limit is not None and saved >= limit:
                    break

                seen_urls.add(it["url"])

                try:
                    abstract = it.get("abstract", "")
                    subtitle = it.get("subtitle", "")

                    # Skip items with very short abstracts
                    if len(abstract) < _MIN_ABSTRACT:
                        print(
                            f"[bra-se-english] Abstract too short ({len(abstract)} chars), "
                            f"skipping: {it['url']}"
                        )
                        continue

                    # --- Fetch detail page for PDF URL ---
                    pdf_url = ""
                    detail_url = _BASE_URL + it["url"]
                    try:
                        time.sleep(self._delay)
                        detail_raw = _curl(detail_url)
                        if detail_raw:
                            pdf_url = _get_pdf_url(detail_raw)
                    except Exception as exc:
                        print(f"[bra-se-english] detail fetch error ({it['url']}): {exc}")

                    # External ID from slug after /archive/
                    slug = (
                        it["url"].split("/archive/", 1)[-1]
                        if "/archive/" in it["url"]
                        else it["url"].lstrip("/")
                    )

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": slug,
                        "title": it.get("title", ""),
                        "authors": json.dumps([], ensure_ascii=False),
                        "abstract": abstract,
                        "category": it.get("category", ""),
                        "keywords": json.dumps([], ensure_ascii=False),
                        "published_date": it.get("date", ""),
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "doi": "",
                        "department": "",
                        "metadata": json.dumps(
                            {"subtitle": subtitle},
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[bra-se-english] Saved {saved}/{limit_str}: "
                        f"{it.get('title', '')[:60]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[bra-se-english] item failed ({it.get('url', '?')}): {exc}")
                    continue

            # --- Progress log every 10 pages ---
            if (page + 1) % 10 == 0:
                print(f"[bra-se-english] page {page + 1}: saved {saved}/{limit_str}")

            page += 1

        print(f"[bra-se-english] Done. Total saved: {saved}")
        return saved
