# -*- coding: utf-8 -*-
"""Crawler for ICN ARTEM Business School — English news/press."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from urllib.parse import urljoin

from crawler.base_crawler import BaseCrawler


class IcnArtemComEnCrawler(BaseCrawler):
    site_id = "icn-artem-com-en"
    site_name = "Custom: icn-artem-com-en"
    base_url = "https://www.icn-artem.com"

    _NEWS_URL = "https://www.icn-artem.com/en/news/"
    _SITEMAP_URL = "https://www.icn-artem.com/post-sitemap.xml"
    _BACKOFF = (1, 3, 9)
    _MIN_ABSTRACT_CHARS = 50

    # ----------------------------------------------------------------
    # curl helper with retry + exponential backoff
    # ----------------------------------------------------------------
    def _curl_get(self, url: str, context: str = "") -> str | None:
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--max-time", "30",
            "-A", self.USER_AGENT,
            url,
        ]
        for attempt, backoff in enumerate(self._BACKOFF):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                if attempt < len(self._BACKOFF) - 1:
                    print(f"[{self.site_id}] empty response ({context}), retry in {backoff}s")
                    time.sleep(backoff)
            except Exception as exc:
                if attempt < len(self._BACKOFF) - 1:
                    print(f"[{self.site_id}] curl error ({context}): {exc}, retry in {backoff}s")
                    time.sleep(backoff)
                else:
                    print(f"[{self.site_id}] curl failed after {len(self._BACKOFF)} attempts ({context}): {exc}")
        return None

    # ----------------------------------------------------------------
    # BeautifulSoup with parser fallback chain
    # ----------------------------------------------------------------
    @staticmethod
    def _make_soup(html: str):
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                from bs4 import BeautifulSoup
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    # ----------------------------------------------------------------
    # Strip HTML tags, normalise whitespace
    # ----------------------------------------------------------------
    @staticmethod
    def _strip_tags(html: str) -> str:
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&[a-zA-Z]+;", "", text)
        return re.sub(r"\s+", " ", text).strip()

    # ----------------------------------------------------------------
    # Discover all English news URLs
    # ----------------------------------------------------------------
    def _sitemap_urls(self) -> list[str]:
        raw = self._curl_get(self._SITEMAP_URL, context="sitemap")
        if not raw:
            return []
        locs = re.findall(r"<loc>(.*?)</loc>", raw)
        # Keep only English posts (slug under /en/ that look like news articles)
        skip_prefixes = (
            "/en/students", "/en/international", "/en/executive-education",
            "/en/business-relations", "/en/news/", "/en/formation",
            "/en/contact", "/en/students/",
        )
        en_posts = []
        for loc in locs:
            if "icn-artem.com/en/" not in loc:
                continue
            path = loc.replace("https://www.icn-artem.com", "")
            if any(path.startswith(p) for p in skip_prefixes):
                continue
            en_posts.append(loc)
        return en_posts

    def _news_page_urls(self) -> list[str]:
        raw = self._curl_get(self._NEWS_URL, context="news list")
        if not raw:
            return []
        try:
            soup = self._make_soup(raw)
        except Exception:
            soup = None
        if not soup:
            # Regex fallback
            return re.findall(
                r'href=["\']( https?://www\.icn-artem\.com/en/[^"\']+)["\']',
                raw,
            )
        urls = []
        for article in soup.find_all("article"):
            for a in article.find_all("a", href=True):
                href = a["href"]
                if "icn-artem.com/en/" in href:
                    urls.append(href)
        return urls

    # ----------------------------------------------------------------
    # Fetch and parse one detail page
    # ----------------------------------------------------------------
    def _parse_detail(self, url: str) -> dict | None:
        raw = self._curl_get(url, context=f"detail")
        if not raw:
            return None

        # --- Post ID from body class (postid-NNNN) ---
        post_id: str | None = None
        body_match = re.search(r"<body[^>]+class=\"([^\"]+)\"", raw)
        if body_match:
            pid = re.search(r"postid-(\d+)", body_match.group(1))
            if pid:
                post_id = pid.group(1)

        # --- JSON-LD structured data ---
        title = ""
        published_date = ""
        author = ""
        category = ""
        schemas = re.findall(
            r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
            raw, re.DOTALL,
        )
        for s in schemas:
            try:
                d = json.loads(s)
                graph = d.get("@graph", [d])
                for item in graph:
                    if item.get("@type") == "Article":
                        title = item.get("headline", "") or title
                        dp = item.get("datePublished", "")
                        if dp:
                            published_date = dp[:10]
                        auth = item.get("author", {})
                        if isinstance(auth, dict):
                            author = auth.get("name", "") or author
                        elif isinstance(auth, str):
                            author = auth
                        secs = item.get("articleSection", [])
                        if isinstance(secs, list) and secs:
                            category = secs[0]
                        elif isinstance(secs, str):
                            category = secs
            except Exception:
                continue

        # --- Title fallback from H1 ---
        if not title:
            h1 = re.search(r"<h1[^>]*>(.*?)</h1>", raw, re.DOTALL)
            if h1:
                title = self._strip_tags(h1.group(1))

        # --- Date fallback from visible date string (DD.MM.YY or DD-MM-YYYY) ---
        if not published_date:
            dm = re.search(r"(\d{2})[.\-](\d{2})[.\-](\d{2,4})", raw)
            if dm:
                d_part, m_part, y_part = dm.groups()
                if len(y_part) == 2:
                    y_part = "20" + y_part
                published_date = f"{y_part}-{m_part}-{d_part}"

        # --- Abstract from <main> paragraphs (BeautifulSoup) ---
        abstract = ""
        try:
            soup = self._make_soup(raw)
            if soup:
                main = soup.find("main")
                if main:
                    paras = []
                    for p in main.find_all("p"):
                        text = p.get_text(separator=" ", strip=True)
                        if len(text) > 40:
                            paras.append(text)
                    abstract = "\n\n".join(paras)
        except Exception as exc:
            print(f"[{self.site_id}] soup parse error for {url}: {exc}")

        # --- Abstract fallback via regex <p> extraction ---
        if not abstract:
            para_matches = re.findall(r"<p[^>]*>(.*?)</p>", raw, re.DOTALL)
            paras = []
            for pm in para_matches:
                text = self._strip_tags(pm)
                if len(text) > 40:
                    paras.append(text)
            abstract = "\n\n".join(paras)

        return {
            "post_id": post_id,
            "title": title,
            "published_date": published_date,
            "author": author,
            "category": category,
            "abstract": abstract,
            "url": url,
        }

    # ----------------------------------------------------------------
    # Main crawl
    # ----------------------------------------------------------------
    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
        SAFETY_CAP = 200

        # Build URL list: news list page first (most recent), then sitemap
        news_urls = self._news_page_urls()
        sitemap_urls = self._sitemap_urls()

        all_urls: list[str] = []
        for u in news_urls + sitemap_urls:
            if u not in seen_urls:
                seen_urls.add(u)
                all_urls.append(u)

        print(f"[{self.site_id}] Discovered {len(all_urls)} unique English news URLs")

        seen_detail: set[str] = set()
        page_count = 0

        for url in all_urls:
            if limit is not None and saved >= limit:
                break
            if url in seen_detail:
                continue
            seen_detail.add(url)

            if time.time() - start_time > MAX_WALL_SECONDS:
                print(f"[{self.site_id}] Approaching 25-minute wall-clock budget, stopping.")
                break

            page_count += 1
            if page_count >= SAFETY_CAP:
                print(f"[{self.site_id}] Safety cap of {SAFETY_CAP} pages reached, stopping.")
                break

            if page_count % 10 == 0:
                limit_str = str(limit) if limit is not None else "∞"
                print(f"[{self.site_id}] page {page_count}: saved {saved}/{limit_str}")

            try:
                time.sleep(self._delay)
                record = self._parse_detail(url)
                if record is None:
                    print(f"[{self.site_id}] item {url} failed: no data returned")
                    continue

                title = record.get("title", "").strip()
                abstract = record.get("abstract", "").strip()
                published_date = record.get("published_date", "")
                author = record.get("author", "")
                category = record.get("category", "")
                post_id = record.get("post_id")

                if not title:
                    print(f"[{self.site_id}] item {url} skipped: no title")
                    continue

                if len(abstract) < self._MIN_ABSTRACT_CHARS:
                    print(
                        f"[{self.site_id}] item {url} skipped: "
                        f"abstract too short ({len(abstract)} chars)"
                    )
                    continue

                # external_id: prefer numeric post_id, else derive from URL slug
                if post_id:
                    external_id = post_id
                else:
                    slug = url.rstrip("/").split("/en/", 1)[-1].strip("/")
                    external_id = re.sub(r"[^a-zA-Z0-9_-]", "-", slug)

                paper = {
                    "site_id": self.site_id,
                    "external_id": external_id,
                    "post_number": post_id,
                    "title": title,
                    "abstract": abstract,
                    "published_date": published_date,
                    "listed_date": published_date,
                    "authors": author,
                    "publisher": "ICN ARTEM Business School",
                    "department": None,
                    "journal": None,
                    "url": url,
                    "pdf_url": None,
                    "keywords": None,
                    "category": category,
                    "doi": None,
                    "original_filename": None,
                    "metadata": json.dumps(
                        {
                            "posted_date": published_date,
                            "originalFilename": None,
                            "node_id": post_id,
                            "articleSection": category,
                        },
                        ensure_ascii=False,
                    ),
                }

                self._save_paper(paper)
                saved += 1
                limit_str = str(limit) if limit is not None else "∞"
                print(f"[{self.site_id}] saved {saved}/{limit_str}: {title[:60]}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {url} failed: {exc}")
                continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
