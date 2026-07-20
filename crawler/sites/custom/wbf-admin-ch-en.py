# -*- coding: utf-8 -*-
"""Crawler for WBF (EAER) English press information.

API: https://d-nsbc-p.admin.ch/v1/search
     Newsbroker content-broker search endpoint, proxied through the WBF
     Nuxt frontend. Returns press releases and news items.

Pagination: offset-based (offset, limit params). Total = pageResults field.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from urllib.parse import urlencode

from crawler.base_crawler import BaseCrawler


class WbfAdminChEnCrawler(BaseCrawler):
    site_id = "wbf-admin-ch-en"
    site_name = "Custom: wbf-admin-ch-en"
    base_url = "https://www.wbf.admin.ch"

    _API_URL = "https://d-nsbc-p.admin.ch/v1/search"
    _START_DATE = "2000-01-01T00:00:00Z"
    _END_DATE = "2030-12-31T23:59:59Z"
    _PAGE_SIZE = 12
    _BACKOFF = (1, 3, 9)
    _CURL_TIMEOUT = 60
    _MAX_PAGES = 200
    _MAX_WALL_SECS = 25 * 60
    _MIN_ABSTRACT_CHARS = 100

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        offset = 0
        page = 0
        start_time = time.time()
        limit_eff = limit if limit is not None else float("inf")

        while saved < limit_eff:
            if time.time() - start_time > self._MAX_WALL_SECS:
                print(f"[{self.site_id}] wall-clock budget exceeded, stopping cleanly")
                break

            if page >= self._MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached, stopping")
                break

            if page % 10 == 0:
                lim_str = str(limit) if limit is not None else "inf"
                print(f"[{self.site_id}] page {page}: saved {saved}/{lim_str}")

            data = self._fetch_list_page(offset, self._PAGE_SIZE)
            if data is None:
                print(f"[{self.site_id}] API returned None on page {page}, stopping")
                break

            items = data.get("items") or []
            if not items:
                print(f"[{self.site_id}] no items on page {page}, done")
                break

            total = data.get("pageResults") or 0

            for item in items:
                if saved >= limit_eff:
                    break

                item_id = item.get("id") or "?"
                try:
                    paper = self._parse_item(item)
                    if paper is None:
                        continue

                    url = paper.get("url") or ""
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self._MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_id} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_id} failed: {exc}")
                    continue

            if offset + self._PAGE_SIZE >= total:
                break

            offset += self._PAGE_SIZE
            page += 1
            time.sleep(self._delay)

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _fetch_list_page(self, offset: int, limit: int):
        params = [
            ("languages", "en"),
            ("newsKinds", "CONTENT_HUB"),
            ("newsKinds", "ONSB"),
            ("publisherIDs", "7"),
            ("start_date", self._START_DATE),
            ("end_date", self._END_DATE),
            ("offset", str(offset)),
            ("limit", str(limit)),
            ("sort", "DESC"),
        ]
        url = f"{self._API_URL}?{urlencode(params)}"

        last_error = ""
        for attempt in range(3):
            try:
                result = subprocess.run(
                    [
                        "curl", "--tls-max", "1.3", "-skL",
                        "--connect-timeout", "15",
                        "--max-time", str(self._CURL_TIMEOUT),
                        "-A", self.USER_AGENT,
                        "-H", f"Referer: {self.base_url}/",
                        "-H", f"Origin: {self.base_url}",
                        "-H", "Accept: application/json",
                        url,
                    ],
                    capture_output=True,
                    timeout=self._CURL_TIMEOUT + 10,
                )
                raw = result.stdout.decode("utf-8", errors="replace").strip()
                if not raw:
                    raise RuntimeError("empty response body")
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise RuntimeError(f"unexpected JSON type: {type(data)}")
                return data
            except Exception as exc:
                last_error = str(exc)
                print(
                    f"[{self.site_id}] list API failed "
                    f"(attempt {attempt + 1}/3, offset={offset}): {last_error}"
                )
                if attempt < 2:
                    wait = self._BACKOFF[attempt]
                    print(f"[{self.site_id}] retrying in {wait}s...")
                    time.sleep(wait)

        print(f"[{self.site_id}] list API failed after 3 attempts: {last_error}")
        return None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_item(self, item: dict):
        item_id = item.get("id") or ""
        lang_group_id = item.get("langGroupId") or ""
        meta = (item.get("content") or {}).get("metadata") or {}

        title = (
            meta.get("metaTitle")
            or item.get("title")
            or ""
        ).strip()
        if not title:
            return None

        abstract = self._pick_abstract(item, meta)

        # Dates
        announce_raw = meta.get("announcementDate") or ""
        published_date = announce_raw[:10] if announce_raw else ""
        listed_raw = item.get("publishDate") or ""
        listed_date = listed_raw[:10]

        url = (
            f"{self.base_url}/en/newnsb/{lang_group_id}"
            if lang_group_id
            else ""
        )

        # post_number: numeric suffix from id like "swr_18299" → "18299"
        m = re.search(r"\d+", item_id)
        post_number = m.group(0) if m else None

        # Keywords from topic IDs
        topic_ids = item.get("topics") or []
        keywords_str = (
            ", ".join(str(t) for t in topic_ids) if topic_ids else None
        )

        # Category
        category = item.get("newsCategory") or ""

        metadata = {
            "posted_date": listed_raw,
            "newsCategory": category,
            "langGroupId": lang_group_id,
            "location": item.get("location"),
            "kind": item.get("kind"),
            "publisherIDs": item.get("publishers") or [],
            "topicIDs": topic_ids,
            "tags": item.get("tags") or [],
            "coPublishers": item.get("coPublishers") or [],
            "lang": item.get("lang"),
            "announcementDate": announce_raw,
            "item_id": item_id,
        }

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": item_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": listed_date,
            "url": url,
            "pdf_url": None,
            "authors": None,
            "publisher": None,
            "department": None,
            "journal": None,
            "doi": None,
            "category": category,
            "keywords": keywords_str,
            "original_filename": None,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _pick_abstract(self, item: dict, meta: dict) -> str:
        # Prefer the top-level description (clean text, 200-600 chars typically)
        for src in (
            item.get("description"),
            meta.get("description"),
            meta.get("metaDescription"),
            meta.get("openGraphDescription"),
        ):
            if src and len(str(src).strip()) >= self._MIN_ABSTRACT_CHARS:
                return str(src).strip()

        # Fall back to stripping HTML from the text[] list
        text_parts = item.get("text")
        if isinstance(text_parts, list) and text_parts:
            try:
                from bs4 import BeautifulSoup
                html = " ".join(str(p) for p in text_parts[:10])
                for parser in ("html5lib", "lxml", "html.parser"):
                    try:
                        soup = BeautifulSoup(html, parser)
                        plain = soup.get_text(" ", strip=True)
                        if plain:
                            return plain[:5000]
                    except Exception:
                        continue
            except Exception:
                pass

        # Final fallback: whatever we have
        for src in (item.get("description"), meta.get("description")):
            if src:
                return str(src).strip()

        return ""
