# -*- coding: utf-8 -*-
"""Crawler for ypes.gr press releases (Δελτία Τύπου).

Uses the WordPress REST API (category 12 = deltia-typoy) which returns full
post content, title, date, and ID without needing to scrape HTML detail pages.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class YpesGrCategoryCrawler(BaseCrawler):
    site_id = "ypes-gr-category"
    site_name = "Custom: ypes-gr-category"
    base_url = "https://www.ypes.gr"

    # REST API endpoint for category 12 (deltia-typoy)
    _API_BASE = "https://www.ypes.gr/wp-json/wp/v2/posts"
    _CATEGORY_ID = 12
    _PER_PAGE = 100

    MAX_PAGES = 200
    BACKOFF = (1, 3, 9)
    CURL_TIMEOUT = 45
    MIN_ABSTRACT_CHARS = 100
    MIN_SAVED_ABSTRACT_CHARS = 100
    MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes

    # curl headers that bypass Akamai CDN (plain curl gets 403, these pass)
    _CURL_HEADERS = [
        "Accept: application/json, */*;q=0.8",
        "Accept-Language: el-GR,el;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control: no-cache",
    ]
    _USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)

    # ------------------------------------------------------------------
    # Public crawl entry point
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_ids: set[str] = set()
        start_wall = time.time()
        limit_label = str(limit) if limit is not None else "∞"

        for page in range(1, self.MAX_PAGES + 1):
            if time.time() - start_wall > self.MAX_WALL_SECONDS:
                print(
                    f"[{self.site_id}] wall-clock limit ({self.MAX_WALL_SECONDS}s) reached "
                    f"at page {page}; exiting cleanly"
                )
                break

            if limit is not None and saved >= limit:
                break

            if page > self.MAX_PAGES:
                print(f"[{self.site_id}] safety page cap ({self.MAX_PAGES}) reached; stopping")
                break

            api_url = (
                f"{self._API_BASE}"
                f"?categories={self._CATEGORY_ID}"
                f"&per_page={self._PER_PAGE}"
                f"&page={page}"
                f"&orderby=date&order=desc"
                f"&_fields=id,slug,title,date,excerpt,content,link"
            )

            raw = self._curl_get(api_url, context=f"API page {page}")
            if not raw:
                print(f"[{self.site_id}] API fetch failed at page {page}; stopping")
                break

            try:
                posts = json.loads(raw.decode("utf-8", errors="replace"))
            except Exception as exc:
                print(f"[{self.site_id}] JSON parse error at page {page}: {exc}; stopping")
                break

            if not isinstance(posts, list) or len(posts) == 0:
                print(f"[{self.site_id}] page {page}: empty response; end of list")
                break

            if page % 10 == 0 or page == 1:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            for post in posts:
                if limit is not None and saved >= limit:
                    break

                post_id = str(post.get("id", ""))
                if not post_id or post_id in seen_ids:
                    continue
                seen_ids.add(post_id)

                try:
                    n = self._process_post(post)
                    if n:
                        saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    url = post.get("link", post_id)
                    print(f"[{self.site_id}] item {url} failed: {exc}; skipping")
                    continue

                time.sleep(self._delay)

        print(f"[{self.site_id}] crawl complete: saved {saved}")
        return saved

    # ------------------------------------------------------------------
    # Per-post processing
    # ------------------------------------------------------------------

    def _process_post(self, post: dict) -> int:
        post_id = str(post.get("id", ""))
        url = post.get("link", "") or ""
        date_raw = post.get("date", "") or ""
        published_date = date_raw[:10] if date_raw else None

        # Title
        title_raw = (post.get("title") or {}).get("rendered", "") or ""
        title = self._strip_html(title_raw).strip()
        if not title:
            title = "(untitled)"

        # Abstract — prefer full content, fall back to excerpt
        content_html = (post.get("content") or {}).get("rendered", "") or ""
        excerpt_html = (post.get("excerpt") or {}).get("rendered", "") or ""

        abstract = self._build_abstract(content_html, excerpt_html)
        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            print(
                f"[{self.site_id}] abstract too short ({len(abstract)} chars) "
                f"for post {post_id} ({url}); skipping"
            )
            return 0

        # PDF extraction from content HTML
        pdf_url, original_filename = self._find_pdf(content_html)

        # metadata — keep raw WP fields
        meta = {
            "wp_id": post.get("id"),
            "slug": post.get("slug"),
        }

        self._save_paper({
            "site_id": self.site_id,
            "external_id": post_id,
            "post_number": post_id,
            "title": title,
            "abstract": abstract,
            "url": url,
            "published_date": published_date,
            "listed_date": published_date,
            "authors": None,
            "publisher": "Υπουργείο Εσωτερικών",
            "department": None,
            "journal": None,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "keywords": None,
            "category": "Δελτία Τύπου",
            "doi": None,
            "metadata": json.dumps(meta, ensure_ascii=False),
        })
        return 1

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_abstract(self, content_html: str, excerpt_html: str) -> str:
        if content_html:
            text = self._strip_html(content_html)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) >= self.MIN_SAVED_ABSTRACT_CHARS:
                return text[:3000]

        if excerpt_html:
            text = self._strip_html(excerpt_html)
            text = re.sub(r"\s+", " ", text).strip()
            # Remove trailing "[…]" marker WP appends to excerpts
            text = re.sub(r"\[\s*[…\.]{1,3}\s*\]\s*$", "", text).strip()
            if len(text) >= self.MIN_ABSTRACT_CHARS:
                return text[:3000]

        return ""

    def _strip_html(self, html: str) -> str:
        if not html:
            return ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser).get_text(separator=" ", strip=True)
            except Exception:
                continue
        # Last-resort: naive tag strip
        return re.sub(r"<[^>]+>", " ", html).strip()

    def _find_pdf(self, content_html: str) -> tuple[str | None, str | None]:
        if not content_html:
            return None, None
        for m in re.finditer(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', content_html, re.I):
            href = m.group(1)
            pdf_url = href if href.startswith("http") else self.base_url + href
            path = urlparse(pdf_url).path
            filename = path.rstrip("/").split("/")[-1] if path else None
            if filename:
                try:
                    filename = filename.encode("latin-1").decode("utf-8")
                except Exception:
                    pass
            return pdf_url, filename or None
        return None, None

    def _curl_get(self, url: str, context: str = "") -> bytes | None:
        header_args: list[str] = []
        for h in self._CURL_HEADERS:
            header_args += ["-H", h]

        for attempt, backoff in enumerate(self.BACKOFF):
            try:
                result = subprocess.run(
                    [
                        "curl",
                        "--tls-max", "1.3",
                        "-sk",
                        "--compressed",
                        "-L",
                        "--max-time", str(self.CURL_TIMEOUT),
                        "-A", self._USER_AGENT,
                    ] + header_args + [url],
                    capture_output=True,
                    timeout=self.CURL_TIMEOUT + 5,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout
                print(
                    f"[{self.site_id}] curl returned {result.returncode} "
                    f"for {context} ({url})"
                )
            except subprocess.TimeoutExpired:
                print(f"[{self.site_id}] curl timeout for {context} ({url})")
            except Exception as exc:
                print(f"[{self.site_id}] curl error for {context}: {exc}")

            if attempt < len(self.BACKOFF) - 1:
                print(f"[{self.site_id}] retrying {context} in {backoff}s...")
                time.sleep(backoff)

        return None
