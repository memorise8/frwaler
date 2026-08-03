# -*- coding: utf-8 -*-
"""Crawler for vlaanderen.be/nieuwsberichten — Flemish Government news articles.

API: POST https://www.vlaanderen.be/api/overview-search
  filter.contentType = {IN: ["NewsArticle"]}
  orderBy = {publicationDate: "DESC"}
  auth: Basic (embedded in front-end JS bundle)

Detail: GET https://www.vlaanderen.be/api/newsarticles/nl/{uuid}?language=nl
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urljoin

from crawler.base_crawler import BaseCrawler


class VlaaanderenNieuwsberichtenCrawler(BaseCrawler):
    site_id = "vlaanderen-be-nieuwsberichten"
    site_name = "Custom: vlaanderen-be-nieuwsberichten"
    base_url = "https://www.vlaanderen.be"

    _LIST_URL = "https://www.vlaanderen.be/api/overview-search"
    _DETAIL_URL = "https://www.vlaanderen.be/api/newsarticles/nl/{identifier}?language=nl"
    # Basic auth from front-end JS bundle (webplatformdev:webplatformdev)
    _AUTH = "Basic d2VicGxhdGZvcm1kZXY6d2VicGxhdGZvcm1kZXY="
    # Root hub scope for Vlaanderen.be
    _HUB_SCOPE = "cc0f4502-9afd-42cf-b71f-31e43937d855"
    _PAGE_SIZE = 20

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, *, timeout: int = 30) -> str | None:
        cmd = [
            "curl", "-skL", "--tls-max", "1.3",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"Authorization: {self._AUTH}",
            "-H", "Accept: application/json",
            "-H", f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        waits = [1, 3, 9]
        last_err = "unknown"
        for attempt in range(3):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=timeout + 10, check=False)
                if r.returncode == 0 and r.stdout:
                    return r.stdout.decode("utf-8", errors="replace")
                last_err = f"exit={r.returncode}"
            except Exception as exc:
                last_err = str(exc)
            if attempt < 2:
                w = waits[attempt]
                print(f"[{self.site_id}] GET failed {attempt+1}/3 ({last_err}); retry in {w}s")
                time.sleep(w)
        print(f"[{self.site_id}] GET {url[:100]} failed after 3 attempts: {last_err}")
        return None

    def _curl_post(self, url: str, body: dict, *, timeout: int = 30) -> str | None:
        body_str = json.dumps(body, ensure_ascii=False)
        cmd = [
            "curl", "-skL", "--tls-max", "1.3",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-X", "POST",
            "-H", f"Authorization: {self._AUTH}",
            "-H", "Content-Type: application/json",
            "-H", "Accept: application/json",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-d", body_str,
            url,
        ]
        waits = [1, 3, 9]
        last_err = "unknown"
        for attempt in range(3):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=timeout + 10, check=False)
                if r.returncode == 0 and r.stdout:
                    return r.stdout.decode("utf-8", errors="replace")
                last_err = f"exit={r.returncode}"
            except Exception as exc:
                last_err = str(exc)
            if attempt < 2:
                w = waits[attempt]
                print(f"[{self.site_id}] POST failed {attempt+1}/3 ({last_err}); retry in {w}s")
                time.sleep(w)
        print(f"[{self.site_id}] POST {url} failed after 3 attempts: {last_err}")
        return None

    # ------------------------------------------------------------------
    # Text helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_html(html_str: str) -> str:
        if not html_str:
            return ""
        text = re.sub(r"<[^>]+>", " ", html_str)
        text = unescape(text)
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def _epoch_to_date(epoch_val) -> str:
        """Unix timestamp (seconds or ms string/int) → YYYY-MM-DD."""
        if not epoch_val:
            return ""
        try:
            ts = int(str(epoch_val).rstrip("0").ljust(1, "0")) if False else int(str(epoch_val))
            # Detect milliseconds (>1e11) vs seconds
            if ts > 100_000_000_000:
                ts = ts // 1000
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            return ""

    # ------------------------------------------------------------------
    # API calls
    # ------------------------------------------------------------------

    def _fetch_list_page(self, offset: int, page_size: int) -> dict | None:
        body = {
            "page": {"offset": offset, "limit": page_size},
            "filter": {
                "visibility": {"hub": self._HUB_SCOPE},
                "contentType": {"IN": ["NewsArticle"]},
            },
            "orderBy": {"publicationDate": "DESC"},
            "resolverContext": {"language": "nl"},
            "predefinedFilter": {},
            "facetFilterKeys": [],
        }
        raw = self._curl_post(self._LIST_URL, body)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"[{self.site_id}] list JSON decode error at offset {offset}: {e}")
            return None

    def _fetch_detail(self, identifier: str) -> dict | None:
        url = self._DETAIL_URL.format(identifier=identifier)
        raw = self._curl_get(url)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return data.get("data", {}).get("newsArticle")
        except json.JSONDecodeError:
            return None

    # ------------------------------------------------------------------
    # Content extraction
    # ------------------------------------------------------------------

    def _build_abstract(self, list_item: dict, detail: dict | None) -> str:
        parts = []

        # Intro from list (most items have this)
        intro_html = (
            list_item.get("intro")
            or (list_item.get("description") or {}).get("raw", "")
        )
        intro_text = self._strip_html(intro_html)
        if intro_text:
            parts.append(intro_text)

        # Stories from detail page
        if detail:
            for story in (detail.get("stories") or []):
                if story.get("type") == "text":
                    content_text = self._strip_html(story.get("content") or "")
                    if content_text and content_text not in parts:
                        parts.append(content_text)

        return "\n\n".join(parts).strip()

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        offset = 0
        seen_urls: set[str] = set()
        page_num = 0
        total_on_server: int | None = None
        start_time = time.time()
        SAFETY_CAP = 200  # max pages

        while True:
            # Time budget: 25 minutes
            if time.time() - start_time > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[{self.site_id}] 25-minute budget reached. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page_num >= SAFETY_CAP:
                print(f"[{self.site_id}] Safety cap of {SAFETY_CAP} pages reached. Stopping.")
                break

            # Fetch list batch
            time.sleep(self._delay)
            result = self._fetch_list_page(offset, self._PAGE_SIZE)
            if not result:
                print(f"[{self.site_id}] List fetch failed at offset {offset}. Stopping.")
                break

            items = result.get("items") or []

            if total_on_server is None:
                total_on_server = result.get("totalItems")
                print(f"[{self.site_id}] Total NewsArticle items on server: {total_on_server}")

            if not items:
                print(f"[{self.site_id}] No items at offset {offset}. Done.")
                break

            # Progress logging every 10 pages
            if page_num % 10 == 0:
                limit_str = str(limit) if limit is not None else "inf"
                print(f"[{self.site_id}] page {page_num}: saved {saved}/{limit_str}")

            for item in items:
                if limit is not None and saved >= limit:
                    break

                url_path = item.get("link") or ""
                url = urljoin(self.base_url, url_path)

                if url in seen_urls:
                    continue
                seen_urls.add(url)

                identifier = item.get("identifier") or ""
                if not identifier:
                    continue

                try:
                    # Fetch detail page for full content
                    time.sleep(self._delay)
                    detail = self._fetch_detail(identifier)

                    abstract = self._build_abstract(item, detail)

                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] skip {identifier}: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    # Dates — list gives seconds, detail gives milliseconds
                    pub_ts = item.get("publicationDate") or (
                        detail.get("publicationDate") if detail else ""
                    )
                    published_date = self._epoch_to_date(pub_ts)

                    # Title
                    title_raw = (
                        (item.get("title") or {}).get("htmlEncoded")
                        or item.get("displayTitle")
                        or ""
                    )
                    title = self._strip_html(title_raw)
                    if not title:
                        continue

                    # Category from contentSubtype
                    ct_sub = item.get("contentSubtype") or {}
                    category = ct_sub.get("name") if ct_sub else None

                    # Publisher from related organisations in detail
                    publishers: list[str] = []
                    if detail:
                        for org in (detail.get("relatedOrganisations") or []):
                            name = org.get("name") or org.get("title") or ""
                            if name:
                                publishers.append(name)

                    # Keywords from tags if present
                    tags: list[str] = []
                    if detail:
                        for tag in (detail.get("tags") or []):
                            t = tag.get("name") or tag.get("label") or ""
                            if t:
                                tags.append(t)

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": identifier,
                        "post_number": identifier,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": published_date,
                        "url": url,
                        "pdf_url": None,
                        "doi": None,
                        "authors": None,
                        "publisher": "; ".join(publishers) if publishers else "Vlaamse overheid",
                        "department": None,
                        "journal": None,
                        "keywords": ", ".join(tags) if tags else None,
                        "category": category,
                        "original_filename": None,
                        "metadata": json.dumps({
                            "posted_date": str(pub_ts) if pub_ts else None,
                            "node_id": identifier,
                            "contentType": item.get("contentType"),
                            "contentSubtype": ct_sub.get("machineName") if ct_sub else None,
                            "hubContentSubtype": (
                                (item.get("hubContentSubtype") or {}).get("machineName")
                                if item.get("hubContentSubtype") else None
                            ),
                            "link": url_path,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    limit_str = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {identifier} failed: {exc}")
                    continue

            offset += len(items)
            page_num += 1

            # End-of-pagination checks
            if result.get("isLastPage"):
                print(f"[{self.site_id}] isLastPage=True at offset {offset}. Done.")
                break

            if total_on_server is not None and offset >= total_on_server:
                print(f"[{self.site_id}] offset {offset} >= total {total_on_server}. Done.")
                break

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
