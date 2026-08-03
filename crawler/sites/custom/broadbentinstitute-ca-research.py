# -*- coding: utf-8 -*-
"""Crawler for Broadbent Institute Research & Publications.

Endpoint: WordPress REST API /wp-json/wp/v2/broadbent_pubs
Listing:  https://broadbentinstitute.ca/research/
"""

import json
import os
import re
import subprocess
import time
from html import unescape
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class BroadbentInstituteCAResearchCrawler(BaseCrawler):
    site_id = "broadbentinstitute-ca-research"
    site_name = "Custom: broadbentinstitute-ca-research"
    base_url = "https://broadbentinstitute.ca"

    _API_BASE = "https://broadbentinstitute.ca/wp-json/wp/v2/broadbent_pubs"
    _PER_PAGE = 100
    _CURL_TIMEOUT = 30
    _RETRIES = 3
    _BACKOFFS = (1, 3, 9)
    _CRAWL_BUDGET_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url):
        """Fetch URL with curl, return raw text or None on failure."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--max-time", str(self._CURL_TIMEOUT),
            "-A", self.USER_AGENT,
            "-H", "Accept: application/json,text/html,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            url,
        ]
        for attempt in range(self._RETRIES):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=self._CURL_TIMEOUT + 10
                )
                raw = result.stdout.decode("utf-8", errors="replace")
                if result.returncode != 0:
                    err = result.stderr.decode("utf-8", errors="replace").strip()
                    raise RuntimeError(err or f"curl exit {result.returncode}")
                if not raw.strip():
                    raise RuntimeError("empty response")
                return raw
            except Exception as exc:
                if attempt < self._RETRIES - 1:
                    wait = self._BACKOFFS[attempt]
                    print(
                        f"[{self.site_id}] fetch attempt {attempt+1}/{self._RETRIES} "
                        f"failed for {url}: {exc}; retrying in {wait}s"
                    )
                    time.sleep(wait)
                else:
                    print(
                        f"[{self.site_id}] fetch failed after {self._RETRIES} "
                        f"attempts for {url}: {exc}"
                    )
        return None

    def _fetch_json(self, url):
        raw = self._curl_get(url)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] JSON decode error for {url}: {exc}")
            return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_soup(raw):
        if not raw:
            return None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _clean_text(value):
        text = unescape(str(value or ""))
        text = text.replace("﻿", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @classmethod
    def _html_to_text(cls, html):
        if not html:
            return ""
        html = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
        soup = cls._make_soup(html)
        if soup is None:
            return cls._clean_text(re.sub(r"<[^>]+>", " ", html))
        for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
            tag.decompose()
        return cls._clean_text(soup.get_text(" ", strip=True))

    @classmethod
    def _extract_pdf_url(cls, content_html):
        """Return first .pdf href found in content HTML, or None."""
        if not content_html:
            return None
        soup = cls._make_soup(content_html)
        if soup:
            for a in soup.find_all("a", href=True):
                href = str(a["href"])
                if ".pdf" in href.lower():
                    return href
        m = re.search(r'href=["\']([^"\']*\.pdf[^"\']*)["\']', content_html, re.I)
        return m.group(1) if m else None

    @staticmethod
    def _original_filename(pdf_url):
        if not pdf_url:
            return None
        path = urlparse(pdf_url).path
        parts = [p for p in path.split("/") if p]
        return parts[-1] if parts else None

    def _extract_authors(self, detail_html):
        """Parse '; '-joined author names from detail page HTML."""
        soup = self._make_soup(detail_html)
        if soup is None:
            return None
        authors = []
        # Primary: Broadbent Institute's custom author link class
        for a in soup.select(".bi-research-author__link"):
            name = self._clean_text(a.get_text())
            if name:
                authors.append(name)
        if authors:
            return "; ".join(authors)
        # Fallback: generic author/byline selectors
        for sel in ["[rel='author']", "[class*='author'] a", "[class*='byline'] a"]:
            for a in soup.select(sel):
                name = self._clean_text(a.get_text())
                if name and len(name) < 100:
                    authors.append(name)
            if authors:
                return "; ".join(authors)
        return None

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls = set()
        limit_or_inf = limit if limit is not None else "inf"
        start_time = time.time()
        MAX_PAGES = 200

        while True:
            # Time budget
            if time.time() - start_time > self._CRAWL_BUDGET_SECS:
                elapsed = time.time() - start_time
                print(f"[{self.site_id}] time budget exceeded ({elapsed:.0f}s), stopping")
                break

            if limit is not None and saved >= limit:
                break

            if page > MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {MAX_PAGES} pages reached")
                break

            if page == 1 or page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            api_url = (
                f"{self._API_BASE}?per_page={self._PER_PAGE}&page={page}"
                f"&_fields=id,slug,title,content,excerpt,date,link,tags"
            )
            posts = self._fetch_json(api_url)
            if posts is None:
                print(f"[{self.site_id}] page {page}: fetch failed, stopping")
                break
            if not isinstance(posts, list) or len(posts) == 0:
                print(f"[{self.site_id}] page {page}: no posts, stopping")
                break

            new_on_page = 0
            for post in posts:
                if limit is not None and saved >= limit:
                    break

                post_url = post.get("link", "")
                if post_url in seen_urls:
                    continue
                seen_urls.add(post_url)
                new_on_page += 1

                post_id = post.get("id")
                try:
                    external_id = str(post_id)
                    post_number = str(post_id)
                    slug = post.get("slug", "")
                    title = self._html_to_text(
                        post.get("title", {}).get("rendered", "")
                    )
                    if not title:
                        print(f"[{self.site_id}] item {external_id} skipped: no title")
                        continue

                    content_html = post.get("content", {}).get("rendered", "")
                    excerpt_html = post.get("excerpt", {}).get("rendered", "")
                    date_iso = (post.get("date") or "")[:10] or None

                    # Abstract: full content text, fallback to excerpt
                    abstract = self._html_to_text(content_html)
                    if len(abstract) < 50:
                        abstract = self._html_to_text(excerpt_html)
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {external_id} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    pdf_url = self._extract_pdf_url(content_html)
                    orig_filename = self._original_filename(pdf_url)

                    # Authors require a detail page fetch
                    time.sleep(self._delay)
                    detail_html = self._curl_get(post_url)
                    authors = self._extract_authors(detail_html) if detail_html else None

                    metadata = {
                        "wordpress_id": post_id,
                        "slug": slug,
                        "posted_date": date_iso,
                        "tags": post.get("tags", []),
                    }
                    if orig_filename:
                        metadata["originalFilename"] = orig_filename

                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "published_date": date_iso,
                        "posted_date": date_iso,
                        "authors": authors,
                        "publisher": "Broadbent Institute",
                        "url": post_url,
                        "pdf_url": pdf_url,
                        "original_filename": orig_filename,
                        "keywords": None,
                        "category": None,
                        "doi": None,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    })
                    saved += 1
                    print(
                        f"[{self.site_id}] saved {saved}/{limit_or_inf}: "
                        f"{title[:70]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {post_id} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: all duplicates, stopping")
                break

            # Fewer results than per_page → last page
            if len(posts) < self._PER_PAGE:
                break

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
