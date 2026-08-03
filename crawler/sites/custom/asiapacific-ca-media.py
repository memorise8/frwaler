# -*- coding: utf-8 -*-
"""Asia Pacific Foundation of Canada — Press Releases crawler.

Target: https://www.asiapacific.ca/media/press-releases

The list page renders ALL press releases in year-grouped expandable sections
(no server-side pagination of the full list). Each detail page carries a
JSON-LD Article block with the full articleBody, datePublished, and name.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime

from crawler.base_crawler import BaseCrawler

_LIST_URL = "https://www.asiapacific.ca/media/press-releases"
_BASE_URL = "https://www.asiapacific.ca"
_PUBLISHER = "Asia Pacific Foundation of Canada"
_BACKOFF = (1, 3, 9)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _curl_get(url: str, retries: int = 3) -> str | None:
    """GET via curl with exponential-backoff retry; returns decoded text or None."""
    for attempt in range(retries):
        try:
            result = subprocess.run(
                [
                    "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
                    "-A",
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36",
                    url,
                ],
                capture_output=True,
                timeout=35,
            )
            text = result.stdout.decode("utf-8", errors="replace")
            if text.strip():
                return text
        except Exception as exc:
            pass
        if attempt < retries - 1:
            wait = _BACKOFF[attempt] if attempt < len(_BACKOFF) else _BACKOFF[-1]
            time.sleep(wait)
    return None


def _make_soup(html: str):
    """Return BeautifulSoup with html5lib → lxml → html.parser fallback."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _parse_date(raw: str) -> str:
    """Normalise 'February 13, 2026' or '2026-02-13' → 'YYYY-MM-DD'."""
    if not raw:
        return ""
    raw = raw.strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", raw):
        return raw[:10]
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return raw


def _clean_text(text: str) -> str:
    """Collapse whitespace and strip common HTML entities."""
    text = text.replace(" ", " ").replace("&nbsp;", " ")
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class AsiaPacificCaMediaCrawler(BaseCrawler):
    """Crawler for Asia Pacific Foundation of Canada press releases."""

    site_id = "asiapacific-ca-media"
    site_name = "Custom: asiapacific-ca-media"
    base_url = _BASE_URL

    # ------------------------------------------------------------------
    # List-page helpers
    # ------------------------------------------------------------------

    def _fetch_list_html(self) -> str | None:
        return _curl_get(_LIST_URL)

    def _extract_article_paths(self, html: str) -> list[str]:
        """Return unique article paths from the year-grouped list sections.

        The list page uses: href = "/media/news-releases/<id>[/slug]"
        (note spaces around =).  We deduplicate while preserving order
        (newest-first).
        """
        # Matches both: href="/..." and href = "/..."
        raw = re.findall(r'href\s*=\s*"(/media/news-releases/[^"]+)"', html)
        seen: set[str] = set()
        unique: list[str] = []
        for h in raw:
            h = h.strip()
            if h not in seen:
                seen.add(h)
                unique.append(h)
        return unique

    # ------------------------------------------------------------------
    # Detail-page helpers
    # ------------------------------------------------------------------

    def _fetch_detail(self, path: str) -> dict | None:
        """Fetch a press release page; extract title, abstract, date."""
        url = _BASE_URL + path
        html = _curl_get(url)
        if not html:
            return None

        # Primary: JSON-LD Article block
        ld_blocks = re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.DOTALL | re.IGNORECASE,
        )
        article_ld: dict = {}
        for block in ld_blocks:
            try:
                data = json.loads(block)
                if isinstance(data, dict) and data.get("@type") == "Article":
                    article_ld = data
                    break
            except (json.JSONDecodeError, AttributeError, ValueError):
                continue

        title: str = article_ld.get("name", "").strip()
        abstract: str = article_ld.get("articleBody", "").strip()
        published_date: str = ""
        if article_ld.get("datePublished"):
            published_date = article_ld["datePublished"][:10]
        description: str = article_ld.get("description", "").strip()

        # Fallbacks via meta tags
        if not title:
            m = re.search(r'property="og:title"\s+content="([^"]+)"', html)
            if m:
                title = m.group(1).strip()

        if not abstract:
            # Try body field via BeautifulSoup
            try:
                soup = _make_soup(html)
                if soup:
                    body_div = soup.find("div", class_=re.compile(r"field--name-body"))
                    if body_div:
                        abstract = body_div.get_text(" ", strip=True)
            except Exception:
                pass

        if not published_date:
            m = re.search(r'"datePublished"\s*:\s*"([^"]{7,})"', html)
            if m:
                published_date = m.group(1)[:10]

        abstract = _clean_text(abstract)

        return {
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "description": description,
            "url": url,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl press releases and persist to DB.

        The site renders all press releases on a single page in year-grouped
        sections (2022-2026, ~50 items total).  We collect the full link list
        in one fetch, then fetch each detail page sequentially.

        limit=None → all items; limit=N → at most N items.
        """
        seen_urls: set[str] = set()
        saved = 0
        start_time = time.time()
        max_wall_seconds = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute budget

        # ---- Phase 1: collect article paths from list page ----------------
        print(f"[{self.site_id}] Fetching list page…")
        html = self._fetch_list_html()
        if not html:
            print(f"[{self.site_id}] Failed to fetch list page. Aborting.")
            return 0

        all_paths = self._extract_article_paths(html)
        print(f"[{self.site_id}] Found {len(all_paths)} unique article links.")

        if not all_paths:
            print(f"[{self.site_id}] No article links found. Aborting.")
            return 0

        # Safety cap: simulate paginated walk so the 200-page / seen-URL
        # logic is consistent with the spec even though this site is one page.
        limit_eff = limit  # None = unlimited
        pages_walked = 1   # we fetched 1 "page" (the list)

        if pages_walked >= 200:
            print(f"[{self.site_id}] Safety cap of 200 pages reached.")

        # ---- Phase 2: fetch detail for each path --------------------------
        for idx, path in enumerate(all_paths, 1):
            # Wall-clock guard
            if time.time() - start_time > max_wall_seconds:
                print(f"[{self.site_id}] Wall-clock budget reached after {idx-1} items. Stopping.")
                break

            # limit guard
            if limit_eff is not None and saved >= limit_eff:
                break

            # Duplicate guard (shouldn't fire, but defensive)
            if path in seen_urls:
                continue
            seen_urls.add(path)

            # Extract numeric node ID from path
            m = re.match(r"/media/news-releases/(\d+)", path)
            if m:
                node_id = m.group(1)
            else:
                # Slug-only fallback (shouldn't happen on this site)
                node_id = re.sub(r"[^0-9a-zA-Z_-]", "", path.split("/")[-1])

            try:
                time.sleep(self._delay)

                detail = self._fetch_detail(path)
                if not detail:
                    print(f"[{self.site_id}] item {path} failed: empty response")
                    continue

                title = detail["title"]
                abstract = detail["abstract"]
                published_date = detail["published_date"]

                if not abstract or len(abstract) < 50:
                    print(
                        f"[{self.site_id}] item {path} skipped: "
                        f"abstract too short ({len(abstract)} chars)"
                    )
                    continue

                paper = {
                    "site_id": self.site_id,
                    "external_id": node_id,
                    "post_number": node_id,
                    "title": title or f"Press Release {node_id}",
                    "abstract": abstract,
                    "published_date": published_date,
                    "listed_date": published_date,
                    "authors": "",
                    "publisher": _PUBLISHER,
                    "department": "",
                    "journal": "",
                    "url": detail["url"],
                    "pdf_url": None,
                    "doi": "",
                    "keywords": "",
                    "category": "Press Release",
                    "original_filename": None,
                    "metadata": json.dumps(
                        {
                            "node_id": node_id,
                            "posted_date": published_date,
                            "description": detail["description"],
                            "category": "Press Release",
                        },
                        ensure_ascii=False,
                    ),
                }

                self._save_paper(paper)
                saved += 1
                limit_str = str(limit_eff) if limit_eff is not None else "inf"
                print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}")

                # Progress every 10 saves
                if saved % 10 == 0:
                    print(
                        f"[{self.site_id}] page {pages_walked}: "
                        f"saved {saved}/{limit_str}"
                    )

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {path} failed: {exc}")
                continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
