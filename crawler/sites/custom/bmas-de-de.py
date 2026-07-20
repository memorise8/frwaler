# -*- coding: utf-8 -*-
"""Crawler for BMAS (Bundesministerium für Arbeit und Soziales) press releases."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from urllib.parse import urlparse

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_LIST_BASE = (
    "https://www.bmas.de/SiteGlobals/Forms/Suche/"
    "Pressemitteilungen_Suche_Formular.html"
)
_LIST_PAGE1 = (
    _LIST_BASE
    + "?view=renderSearchResults&documentType_=pressrelease"
    "&showNoDocType=true&pageNo=0&queryResultId=null"
)

# Paragraphs that are boilerplate (subscribe prompts, etc.) — skip these.
_NOISE_FRAGMENTS = (
    "Newsletter",
    "E-Mail-Postfach",
    "Erhalten Sie Meldungen",
    "Pressemitteilungen des BMAS direkt",
)

_MAX_PAGES = 200
_MAX_SECONDS = 25 * 60  # 25-minute wall-clock budget


class BmasDeDE(BaseCrawler):
    site_id = "bmas-de-de"
    site_name = "Custom: bmas-de-de"
    base_url = "https://www.bmas.de"

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """GET via curl with 3-attempt exponential backoff. Returns decoded text."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: de-DE,de;q=0.9",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                body = result.stdout
                if body and body.strip():
                    return body.decode("utf-8", errors="replace")
                wait = (attempt + 1) ** 2
                print(
                    f"[bmas-de-de] empty response (attempt {attempt+1}/3),"
                    f" retry in {wait}s: {url[:80]}"
                )
                time.sleep(wait)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                wait = (attempt + 1) ** 2
                print(
                    f"[bmas-de-de] curl error (attempt {attempt+1}/3): {exc},"
                    f" retry in {wait}s"
                )
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # List-page helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _page_url(page: int, uuid: str) -> str:
        """Build the URL for page N (1-based) of the search results."""
        if page == 1:
            return _LIST_PAGE1
        gtp = f"%2526{uuid}_list%253D{page}"
        return (
            f"{_LIST_BASE}?gtp={gtp}"
            "&queryResultId=null&view=renderSearchResults"
            "&documentType_=pressrelease&showNoDocType=true&pageNo=0"
        )

    @staticmethod
    def _extract_uuid(html: str) -> str | None:
        m = re.search(r"gtp=%2526([a-f0-9-]{36})_list%253D", html)
        return m.group(1) if m else None

    @staticmethod
    def _extract_last_page(html: str) -> int:
        pages = re.findall(r"_list%253D(\d+)", html)
        return max((int(p) for p in pages), default=1)

    @staticmethod
    def _parse_teasers(html: str) -> list[dict]:
        """Extract press-release entries from a list page."""
        items = []
        for teaser in re.findall(
            r"<pp-teaser[^>]*>.*?</pp-teaser>", html, re.DOTALL
        ):
            # Date
            date_m = re.search(r'datetime="([\d-]+)"', teaser)
            date = date_m.group(1) if date_m else ""

            # URL — strip CMS tracking params
            link_m = re.search(
                r'pp-link\s+href="(https://www\.bmas\.de/DE/[^"]+)"', teaser
            )
            if not link_m:
                link_m = re.search(
                    r'href="(https://www\.bmas\.de/DE/Service/Presse/[^"]+)"',
                    teaser,
                )
            if not link_m:
                continue
            url = re.sub(r"\?cms_.*$", "", link_m.group(1))

            # Title
            h3_m = re.search(r"<h3[^>]*>(.*?)</h3>", teaser, re.DOTALL)
            title = (
                re.sub(r"<[^>]+>", "", h3_m.group(1)).strip() if h3_m else ""
            )

            # Category tag
            tag_m = re.search(
                r"<pp-tag[^>]*>.*?<div[^>]*>(.*?)</div>", teaser, re.DOTALL
            )
            category = (
                re.sub(r"<[^>]+>", "", tag_m.group(1)).strip() if tag_m else ""
            )

            # Teaser snippet
            snip_m = re.search(r'<p class="text">(.*?)</p>', teaser, re.DOTALL)
            snippet = ""
            if snip_m:
                snippet = re.sub(r"<[^>]+>", "", snip_m.group(1))
                snippet = re.sub(r"\s+", " ", snippet).strip()

            if title and url:
                items.append(
                    {
                        "title": title,
                        "url": url,
                        "date": date,
                        "category": category,
                        "snippet": snippet,
                    }
                )
        return items

    # ------------------------------------------------------------------
    # Detail-page helpers
    # ------------------------------------------------------------------

    def _fetch_detail(self, url: str) -> dict:
        """Fetch a press release and extract title, date, abstract."""
        raw = self._curl_get(url)
        if not raw:
            return {}

        # Work inside the main-content section to avoid nav clutter
        idx = raw.find('id="main-content"')
        chunk = raw[idx:idx + 40_000] if idx >= 0 else raw[:40_000]

        # Meta description — compact, clean abstract fallback
        meta_m = re.search(r'<meta name="description" content="([^"]+)"', raw)
        meta_desc = meta_m.group(1) if meta_m else ""

        # Collect substantive paragraphs
        para_texts: list[str] = []
        for raw_p in re.findall(
            r'<p[^>]*class="text"[^>]*>(.*?)</p>', chunk, re.DOTALL
        ):
            text = re.sub(r"<[^>]+>", "", raw_p)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) < 30:
                continue
            if any(frag in text for frag in _NOISE_FRAGMENTS):
                continue
            para_texts.append(text)

        abstract = "\n\n".join(para_texts) if para_texts else meta_desc

        # Canonical date from <time datetime="...">
        time_m = re.search(r'<time[^>]*datetime="([\d-]+)"', chunk)
        date = time_m.group(1) if time_m else ""

        # H1 title (more reliable than teaser h3)
        h1_m = re.search(r"<h1[^>]*>(.*?)</h1>", chunk, re.DOTALL)
        h1 = re.sub(r"<[^>]+>", "", h1_m.group(1)).strip() if h1_m else ""

        return {
            "abstract": abstract,
            "meta_desc": meta_desc,
            "date": date,
            "h1": h1,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        # --- Page 1: discover UUID and last page ----------------------
        print("[bmas-de-de] Fetching page 1 …")
        raw1 = self._curl_get(_LIST_PAGE1)
        if not raw1:
            print("[bmas-de-de] Failed to fetch page 1. Aborting.")
            return 0

        uuid = self._extract_uuid(raw1)
        last_page = self._extract_last_page(raw1)
        print(f"[bmas-de-de] uuid={uuid}  last_page={last_page}")

        page = 1
        current_raw = raw1

        while True:
            # Time-budget guard
            elapsed = time.time() - start_time
            if elapsed > _MAX_SECONDS:
                print(
                    f"[bmas-de-de] 25-minute budget reached at page {page}. Stopping."
                )
                break

            if limit is not None and saved >= limit:
                break

            # Safety cap
            if page > _MAX_PAGES:
                print(f"[bmas-de-de] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break

            teasers = self._parse_teasers(current_raw)
            if not teasers:
                print(f"[bmas-de-de] No teasers on page {page}. Done.")
                break

            new_on_page = 0

            for item in teasers:
                if limit is not None and saved >= limit:
                    break

                url = item["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_on_page += 1

                try:
                    time.sleep(self._delay)
                    detail = self._fetch_detail(url)

                    abstract = detail.get("abstract") or item.get("snippet", "")
                    if len(abstract) < 50:
                        print(
                            f"[bmas-de-de] Skip (abstract too short, "
                            f"{len(abstract)} chars): {url}"
                        )
                        continue

                    title = detail.get("h1") or item["title"]
                    published_date = detail.get("date") or item.get("date", "")
                    listed_date = item.get("date", "")

                    # Derive external_id from URL path: "2026/slug-name"
                    path = urlparse(url).path
                    slug = path.rstrip("/").split("/")[-1].replace(".html", "")
                    year_m = re.search(r"/(\d{4})/", path)
                    year = year_m.group(1) if year_m else ""
                    external_id = f"{year}/{slug}" if year else slug

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": slug,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": listed_date,
                        "url": url,
                        "pdf_url": None,
                        "authors": None,
                        "publisher": "Bundesministerium für Arbeit und Soziales",
                        "department": None,
                        "journal": None,
                        "keywords": item.get("category") or None,
                        "category": item.get("category", ""),
                        "doi": None,
                        "original_filename": None,
                        "metadata": json.dumps(
                            {
                                "snippet": item.get("snippet", ""),
                                "meta_description": detail.get("meta_desc", ""),
                                "posted_date": listed_date,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[bmas-de-de] saved {saved}/{limit_str}: {title[:70]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[bmas-de-de] item failed ({url}): {exc}")
                    continue

            if new_on_page == 0:
                print(
                    f"[bmas-de-de] No new items on page {page} (all duplicates). Done."
                )
                break

            if page % 10 == 0:
                print(
                    f"[bmas-de-de] page {page}: saved {saved}/{limit_str}"
                )

            if page >= last_page:
                print(f"[bmas-de-de] Reached last page ({last_page}). Done.")
                break

            # Advance to next page
            page += 1
            if not uuid:
                print("[bmas-de-de] No pagination UUID found. Cannot continue.")
                break

            print(f"[bmas-de-de] Fetching page {page} …")
            time.sleep(self._delay)
            current_raw = self._curl_get(self._page_url(page, uuid))
            if not current_raw:
                print(f"[bmas-de-de] Failed to fetch page {page}. Stopping.")
                break

        print(f"[bmas-de-de] Done. Total saved: {saved}")
        return saved
