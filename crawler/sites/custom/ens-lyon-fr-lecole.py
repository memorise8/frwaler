# -*- coding: utf-8 -*-
"""Crawler for ENS Lyon press releases (communiqués de presse).

Target: https://www.ens-lyon.fr/lecole/medias-et-presse/communiques-de-presse
Structure:
  List pages: ?page=N (0-based), 12 articles per page, ~20 pages total.
  Each article card: <article data-history-node-id="NODEID"> with a link.
  Detail page: h1 title, .field--name-field-article-date date,
               .field--name-field-article-resume abstract, optional PDF link.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import unquote, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class EnsLyonFrLecoleCrawler(BaseCrawler):
    site_id = "ens-lyon-fr-lecole"
    site_name = "Custom: ens-lyon-fr-lecole"
    base_url = "https://www.ens-lyon.fr"

    LIST_URL = "https://www.ens-lyon.fr/lecole/medias-et-presse/communiques-de-presse"
    PUBLISHER = "École normale supérieure de Lyon"
    BACKOFF_SECONDS = (1, 3, 9)
    CURL_TIMEOUT = 45
    MIN_ABSTRACT_CHARS = 100
    MAX_PAGES = 200
    WALL_CLOCK_LIMIT = 25 * 60  # 25 minutes

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        start_time = time.monotonic()

        page_num = 0
        while True:
            if limit is not None and saved >= limit:
                break

            elapsed = time.monotonic() - start_time
            if elapsed > self.WALL_CLOCK_LIMIT:
                print(f"[{self.site_id}] wall-clock limit reached ({elapsed:.0f}s); stopping")
                break

            if page_num >= self.MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {self.MAX_PAGES} pages reached; stopping")
                break

            if page_num % 10 == 0:
                limit_str = str(limit) if limit is not None else "inf"
                print(f"[{self.site_id}] page {page_num}: saved {saved}/{limit_str}")

            list_url = f"{self.LIST_URL}?page={page_num}"
            raw = self._curl_get(list_url, context=f"list page {page_num}")
            if not raw:
                print(f"[{self.site_id}] failed to fetch list page {page_num}; stopping")
                break

            soup = self._make_soup(raw, context=f"list page {page_num}")
            if soup is None:
                print(f"[{self.site_id}] failed to parse list page {page_num}; stopping")
                break

            articles = soup.select("article[data-history-node-id]")
            if not articles:
                print(f"[{self.site_id}] page {page_num}: no articles found; end of pagination")
                break

            new_on_page = 0
            for article in articles:
                if limit is not None and saved >= limit:
                    break

                node_id = article.get("data-history-node-id", "").strip()
                link_el = article.select_one("a[href]")
                if not link_el:
                    continue

                href = (link_el.get("href") or "").strip()
                # Strip query params (e.g. ?ctx=contexte) to get the canonical URL
                parsed = urlparse(urljoin(self.base_url, href))
                detail_url = urlunparse(parsed._replace(query="", fragment=""))

                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                try:
                    ok = self._process_article(node_id, detail_url)
                    if ok:
                        saved += 1
                        limit_str = str(limit) if limit is not None else "inf"
                        print(f"[{self.site_id}] saved {saved}/{limit_str}: node {node_id}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item node_{node_id} failed: {exc}")
                    continue

                time.sleep(self._delay)

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page_num}: all URLs already seen; stopping")
                break

            page_num += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Per-article processing
    # ------------------------------------------------------------------

    def _process_article(self, node_id, detail_url):
        """Fetch and parse one article detail page. Returns True if saved."""
        raw = self._curl_get(detail_url, context=f"node {node_id}")
        if not raw:
            print(f"[{self.site_id}] node {node_id}: fetch failed; skipping")
            return False

        soup = self._make_soup(raw, context=f"node {node_id}")
        if soup is None:
            print(f"[{self.site_id}] node {node_id}: parse failed; skipping")
            return False

        # Title
        h1 = soup.select_one("h1")
        title = self._node_text(h1)
        if not title:
            print(f"[{self.site_id}] node {node_id}: no title; skipping")
            return False

        # Date: element text is "Date de publication23/04/2026"
        date_el = soup.select_one(".field--name-field-article-date")
        date_raw = self._node_text(date_el) if date_el else ""
        published_date = self._parse_date_dmy(date_raw)

        # Abstract: .field--name-field-article-resume (typically ~500+ chars)
        resume_el = soup.select_one(".field--name-field-article-resume")
        abstract = self._node_text(resume_el) if resume_el else ""

        # Fallback: body field
        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            body_el = soup.select_one(".field--name-body")
            if body_el:
                body_text = self._node_text(body_el)
                if len(body_text) > len(abstract):
                    abstract = body_text

        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            print(
                f"[{self.site_id}] node {node_id}: abstract too short "
                f"({len(abstract)} chars); skipping"
            )
            return False

        # PDF link: first <a href> that contains .pdf
        pdf_url = None
        original_filename = None
        for a in soup.select("a[href]"):
            href = (a.get("href") or "").strip()
            if ".pdf" in href.lower():
                pdf_url = urljoin(self.base_url, href)
                raw_fn = href.split("/")[-1].split("?")[0]
                try:
                    original_filename = unquote(raw_fn)
                except Exception:
                    original_filename = raw_fn
                break

        # Category
        cat_el = soup.select_one(".field--name-field-article-type")
        category = self._node_text(cat_el) if cat_el else None

        # Keywords / tags
        tag_items = soup.select(".field--name-field-tags .field__item")
        if not tag_items:
            tag_items = soup.select("a[rel=tag]")
        keywords = (
            ", ".join(self._node_text(t) for t in tag_items if self._node_text(t)) or None
        )

        external_id = node_id if node_id else detail_url.rstrip("/").split("/")[-1]

        paper = {
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": node_id if node_id else None,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": published_date,
            "url": detail_url,
            "pdf_url": pdf_url,
            "publisher": self.PUBLISHER,
            "authors": None,
            "keywords": keywords,
            "doi": None,
            "department": None,
            "category": category,
            "journal": None,
            "original_filename": original_filename,
            "metadata": json.dumps(
                {
                    "posted_date": date_raw,
                    "originalFilename": original_filename or "",
                    "node_id": node_id,
                    "category": category or "",
                },
                ensure_ascii=False,
            ),
        }

        self._save_paper(paper)
        return True

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url, context="request"):
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--connect-timeout", "15",
            "--max-time", str(self.CURL_TIMEOUT),
            "-A", self.USER_AGENT,
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: fr-FR,fr;q=0.9,en;q=0.7",
        ]
        cmd.append(url)

        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=self.CURL_TIMEOUT + 10
                )
                body = (result.stdout or b"").decode("utf-8", errors="replace")
                if result.returncode != 0:
                    stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if not body.strip():
                    raise RuntimeError("empty response")
                return body
            except Exception as exc:
                last_error = str(exc)
                print(f"[{self.site_id}] {context} attempt {attempt}/3: {last_error}")
                if attempt < 3:
                    wait = self.BACKOFF_SECONDS[attempt - 1]
                    print(f"[{self.site_id}] retrying in {wait}s...")
                    time.sleep(wait)

        print(f"[{self.site_id}] {context} failed after 3 attempts: {last_error}")
        return None

    # ------------------------------------------------------------------
    # Parsing utilities
    # ------------------------------------------------------------------

    def _make_soup(self, raw, context="HTML"):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                last_exc = exc
                print(f"[{self.site_id}] BS4({parser}) failed for {context}: {exc}")
        print(f"[{self.site_id}] all parsers failed for {context}: {last_exc}")
        return None

    @staticmethod
    def _node_text(node):
        if node is None:
            return ""
        text = unescape(node.get_text(" ", strip=True))
        return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()

    @classmethod
    def _parse_date_dmy(cls, value):
        """Parse DD/MM/YYYY → YYYY-MM-DD from strings like 'Date de publication23/04/2026'."""
        if not value:
            return ""
        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", value)
        if m:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return f"{y:04d}-{mo:02d}-{d:02d}"
        return ""
