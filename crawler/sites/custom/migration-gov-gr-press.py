# -*- coding: utf-8 -*-
"""Crawler for migration.gov.gr press releases (Δελτία Τύπου).

Uses the WordPress REST API (/wp/v2/posts?categories=23) which returns
full post content — no detail-page fetches needed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class MigrationGovGrPressCrawler(BaseCrawler):
    site_id = "migration-gov-gr-press"
    site_name = "Custom: migration-gov-gr-press"
    base_url = "https://migration.gov.gr"

    _API_BASE = "https://migration.gov.gr/wp-json/wp/v2/posts"
    _CATEGORY_ID = 23          # "Δελτία Τύπου"
    _PER_PAGE = 10
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _BACKOFF = (1, 3, 9)
    _CURL_TIMEOUT = 45
    _MIN_ABSTRACT_CHARS = 50   # below this → skip entirely
    _MIN_SAVED_ABSTRACT_CHARS = 100  # below this → skip save
    _PUBLISHER = "Υπουργείο Μετανάστευσης και Ασύλου"
    _WALL_CLOCK_BUDGET_S = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes

    _API_FIELDS = "id,date,modified,slug,link,title,content,excerpt"

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl press releases via WP REST API and persist to DB."""
        saved = 0
        page = 1
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        while page <= self._MAX_PAGES:
            elapsed = time.time() - start_time
            if elapsed > self._WALL_CLOCK_BUDGET_S:
                print(f"[{self.site_id}] wall-clock budget reached ({elapsed:.0f}s); stopping")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            api_url = (
                f"{self._API_BASE}"
                f"?categories={self._CATEGORY_ID}"
                f"&per_page={self._PER_PAGE}"
                f"&page={page}"
                f"&_fields={self._API_FIELDS}"
                f"&orderby=date&order=desc"
            )

            raw = self._curl_get(api_url, context=f"API page {page}")
            if raw is None:
                print(f"[{self.site_id}] API request failed at page {page}; stopping")
                break

            try:
                posts = json.loads(raw)
            except (ValueError, TypeError) as exc:
                print(f"[{self.site_id}] JSON parse error at page {page}: {exc}; stopping")
                break

            if not isinstance(posts, list) or len(posts) == 0:
                print(f"[{self.site_id}] no posts at page {page}; stopping")
                break

            print(f"[{self.site_id}] page {page}: fetched {len(posts)} posts")

            for idx, post in enumerate(posts, start=1):
                if limit is not None and saved >= limit:
                    break

                item_label = f"p{page}#{idx}"
                try:
                    post_url = (post.get("link") or "").strip()
                    if not post_url:
                        continue
                    if post_url in seen_urls:
                        continue
                    seen_urls.add(post_url)

                    post_id = post.get("id")
                    if not post_id:
                        continue

                    title_raw = (post.get("title") or {}).get("rendered") or ""
                    title = re.sub(r"\s+", " ", unescape(title_raw)).strip()
                    if not title:
                        print(f"[{self.site_id}] {item_label} skipped: no title")
                        continue

                    content_html = (post.get("content") or {}).get("rendered") or ""
                    excerpt_html = (post.get("excerpt") or {}).get("rendered") or ""

                    abstract = self._html_to_text(content_html)
                    if len(abstract) < self._MIN_ABSTRACT_CHARS:
                        abstract = self._html_to_text(excerpt_html)

                    if len(abstract) < self._MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue
                    if len(abstract) < self._MIN_SAVED_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] {item_label} skipped: "
                            f"abstract below save threshold ({len(abstract)} chars)"
                        )
                        continue

                    date_raw = post.get("date") or ""
                    published_date = date_raw[:10] if len(date_raw) >= 10 else ""

                    pdf_url = self._find_pdf_url(content_html)
                    original_filename = self._filename_from_url(pdf_url) if pdf_url else None

                    metadata = {
                        "wp_post_id": post_id,
                        "slug": post.get("slug") or "",
                        "modified": post.get("modified") or "",
                        "posted_date": published_date,
                        "source": "WP REST API /wp-json/wp/v2/posts?categories=23",
                        "category_id": self._CATEGORY_ID,
                    }

                    paper = {
                        "site_id": self.site_id,
                        "external_id": str(post_id),
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "url": post_url,
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "department": self._PUBLISHER,
                        "category": "Δελτία Τύπου",
                        "authors": json.dumps([], ensure_ascii=False),
                        "keywords": json.dumps(
                            ["μετανάστευση", "άσυλο", "δελτίο τύπου"],
                            ensure_ascii=False,
                        ),
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {title[:80]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if len(posts) < self._PER_PAGE:
                print(f"[{self.site_id}] last page (only {len(posts)} posts returned)")
                break

            if page >= self._MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached")
                break

            page += 1
            time.sleep(self._delay)

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, context: str = "request") -> str | None:
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--connect-timeout", "15",
            "--max-time", str(self._CURL_TIMEOUT),
            "-A", self.USER_AGENT,
            "-H", "Accept: application/json",
            "-H", "Accept-Language: el-GR,el;q=0.9,en;q=0.7",
            url,
        ]
        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=self._CURL_TIMEOUT + 10
                )
                stdout = (result.stdout or b"").decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                if result.returncode != 0:
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if not stdout.strip():
                    raise RuntimeError("empty response")
                return stdout
            except Exception as exc:
                last_error = str(exc)
                print(
                    f"[{self.site_id}] {context} attempt {attempt}/3 failed: {last_error}"
                )
                if attempt < 3:
                    wait = self._BACKOFF[attempt - 1]
                    print(f"[{self.site_id}] retrying in {wait}s...")
                    time.sleep(wait)

        print(f"[{self.site_id}] {context} failed after 3 attempts: {last_error}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _html_to_text(self, html: str) -> str:
        if not html:
            return ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                soup = BeautifulSoup(html, parser)
                for bad in soup.find_all(["script", "style", "noscript"]):
                    bad.decompose()
                text = soup.get_text(" ", strip=True)
                text = unescape(text)
                text = re.sub(r"\s+", " ", text).strip()
                return text
            except Exception:
                continue
        # Manual fallback if all parsers fail
        text = re.sub(r"<[^>]+>", " ", html)
        text = unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _find_pdf_url(html: str) -> str | None:
        if not html:
            return None
        matches = re.findall(
            r'href=["\']([^"\']*\.pdf(?:[?#][^"\']*)?)["\']', html, re.I
        )
        if not matches:
            return None
        url = matches[0].strip()
        if not url.startswith("http"):
            url = urljoin("https://migration.gov.gr", url)
        return url

    @staticmethod
    def _filename_from_url(url: str | None) -> str | None:
        if not url:
            return None
        path = url.split("?")[0].split("#")[0]
        part = path.rstrip("/").rsplit("/", 1)[-1]
        return part if part.lower().endswith(".pdf") else None
