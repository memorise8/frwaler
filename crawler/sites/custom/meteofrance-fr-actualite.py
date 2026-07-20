# -*- coding: utf-8 -*-
"""Crawler for meteofrance.fr publications.

Structure:
  Starting page → <a class="bg"> links to category pages
  Each category page → <div class="mf_paragraph editorial_section rte"> (h2 + p)
                        followed by <div class="mf_paragraph files"> (PDF links)
  Each (editorial_section, files) pair = one document.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import unicodedata
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class MeteoFranceFrActualiteCrawler(BaseCrawler):
    site_id = "meteofrance-fr-actualite"
    site_name = "Custom: meteofrance-fr-actualite"
    base_url = "https://meteofrance.fr"

    START_URL = "https://meteofrance.fr/actualite/publications/les-publications-de-meteo-france"
    BACKOFF_SECONDS = (1, 3, 9)
    CURL_TIMEOUT = 45
    MIN_ABSTRACT_CHARS = 50
    MAX_PAGES = 200
    WALL_CLOCK_LIMIT = 25 * 60  # 25 minutes in seconds

    MONTHS_FR = {
        "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4,
        "mai": 5, "juin": 6, "juillet": 7, "aout": 8,
        "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12,
    }

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Discover category pages then parse each one for section+PDF pairs."""
        saved = 0
        seen_urls = set()
        start_time = time.monotonic()

        category_urls = self._discover_category_urls()
        if not category_urls:
            print(f"[{self.site_id}] no category pages found at {self.START_URL}; stopping")
            return 0

        print(f"[{self.site_id}] discovered {len(category_urls)} category pages")

        for page_num, cat_url in enumerate(category_urls):
            if limit is not None and saved >= limit:
                break

            elapsed = time.monotonic() - start_time
            if elapsed > self.WALL_CLOCK_LIMIT:
                print(f"[{self.site_id}] wall-clock limit reached ({elapsed:.0f}s); stopping")
                break

            if page_num >= self.MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {self.MAX_PAGES} pages reached; stopping")
                break

            if cat_url in seen_urls:
                continue
            seen_urls.add(cat_url)

            if page_num % 10 == 0:
                limit_str = str(limit) if limit is not None else "inf"
                print(f"[{self.site_id}] page {page_num}: saved {saved}/{limit_str}")

            raw = self._curl_get(cat_url, context=f"category page {page_num}")
            if not raw:
                print(f"[{self.site_id}] failed to fetch {cat_url}; skipping")
                continue

            soup = self._make_soup(raw, context=f"category page {page_num}")
            if soup is None:
                print(f"[{self.site_id}] failed to parse {cat_url}; skipping")
                continue

            sections = self._parse_category_page(soup, cat_url)
            if not sections:
                print(f"[{self.site_id}] no sections at {cat_url}; skipping")
                continue

            cat_label = cat_url.rstrip("/").split("/")[-1]
            print(
                f"[{self.site_id}] page {page_num} ({cat_label}): "
                f"{len(sections)} sections found"
            )

            for item_idx, section in enumerate(sections):
                if limit is not None and saved >= limit:
                    break

                try:
                    dedup_key = section.get("pdf_url") or f"{cat_url}#{section['title']}"
                    if dedup_key in seen_urls:
                        continue
                    seen_urls.add(dedup_key)

                    abstract = section.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {page_num}/{item_idx} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    pdf_url = section.get("pdf_url") or None
                    original_filename = None
                    if pdf_url:
                        original_filename = pdf_url.split("/")[-1].split("?")[0]

                    external_id = section["external_id"]

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": self._post_number(external_id, section["title"]),
                        "title": section["title"],
                        "abstract": abstract,
                        "published_date": section.get("published_date") or "",
                        "posted_date": section.get("listed_date") or "",
                        "url": cat_url,
                        "pdf_url": pdf_url,
                        "publisher": "Météo-France",
                        "authors": None,
                        "keywords": None,
                        "doi": None,
                        "department": None,
                        "original_filename": original_filename,
                        "metadata": json.dumps({
                            "posted_date": section.get("listed_date") or "",
                            "originalFilename": original_filename or "",
                            "category_url": cat_url,
                            "article_title": section.get("article_title") or "",
                            "article_date": section.get("article_date") or "",
                            "section_index": item_idx,
                            "category": section.get("category") or "",
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    limit_str = str(limit) if limit is not None else "inf"
                    print(
                        f"[{self.site_id}] saved {saved}/{limit_str}: "
                        f"{section['title'][:80]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {page_num}/{item_idx} failed: {exc}")
                    continue

            time.sleep(self._delay)

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _discover_category_urls(self):
        raw = self._curl_get(self.START_URL, context="start page", referer=self.base_url + "/")
        if not raw:
            return []
        soup = self._make_soup(raw, context="start page")
        if soup is None:
            return []

        urls = []
        seen = set()
        for link in soup.find_all("a", class_="bg"):
            href = (link.get("href") or "").strip()
            if not href:
                continue
            full_url = urljoin(self.base_url, href)
            if full_url not in seen:
                seen.add(full_url)
                urls.append(full_url)
        return urls

    # ------------------------------------------------------------------
    # Page parsing
    # ------------------------------------------------------------------

    def _parse_category_page(self, soup, cat_url):
        """Return list of section dicts from the main article on a category page."""
        sections = []

        main_article = soup.find("article", class_="article_content")
        if main_article is None:
            return []

        h1_node = main_article.find("h1")
        article_title = self._node_text(h1_node)

        date_node = main_article.find("span", class_="date")
        article_date_raw = self._node_text(date_node)
        article_date_iso = self._parse_date_dmy(article_date_raw)

        category = cat_url.rstrip("/").split("/")[-1]

        all_paras = main_article.find_all("div", class_="mf_paragraph")

        i = 0
        while i < len(all_paras):
            para = all_paras[i]
            para_classes = set(para.get("class", []))

            if "editorial_section" not in para_classes:
                i += 1
                continue

            h2_node = para.find("h2")
            if h2_node is None:
                i += 1
                continue

            section_title = self._node_text(h2_node)
            if not section_title:
                i += 1
                continue

            abstract_parts = [
                self._node_text(p)
                for p in para.find_all("p")
                if self._node_text(p)
            ]
            abstract = "\n\n".join(abstract_parts)

            # Look for a following files div (within next 2 para slots)
            pdf_url = None
            j = i + 1
            while j < len(all_paras) and j <= i + 2:
                next_para = all_paras[j]
                next_classes = set(next_para.get("class", []))
                if "files" in next_classes:
                    pdf_link = next_para.find("a", href=re.compile(r"\.pdf", re.I))
                    if pdf_link:
                        href = (pdf_link.get("href") or "").strip()
                        pdf_url = urljoin(self.base_url, href)
                    break
                if "editorial_section" in next_classes:
                    break
                j += 1

            published_date = self._parse_date_from_title(section_title) or article_date_iso

            if pdf_url:
                fn = pdf_url.split("/")[-1].split("?")[0]
                external_id = re.sub(r"\.pdf$", "", fn, flags=re.I)
            else:
                slug = re.sub(r"[^\w]+", "-", section_title.lower()).strip("-")
                external_id = f"{category}-{slug[:60]}"

            sections.append({
                "title": section_title,
                "abstract": abstract,
                "published_date": published_date,
                "listed_date": article_date_iso,
                "listed_date_raw": article_date_raw,
                "pdf_url": pdf_url,
                "external_id": external_id,
                "article_title": article_title,
                "article_date": article_date_iso,
                "category": category,
            })

            i += 1

        return sections

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url, context="request", referer=None):
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--connect-timeout", "15",
            "--max-time", str(self.CURL_TIMEOUT),
            "-A", self.USER_AGENT,
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: fr-FR,fr;q=0.9,en;q=0.7",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
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
                print(
                    f"[{self.site_id}] {context} attempt {attempt}/3: {last_error}"
                )
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
        """Parse DD/MM/YYYY → YYYY-MM-DD."""
        if not value:
            return ""
        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", value)
        if m:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return f"{y:04d}-{mo:02d}-{d:02d}"
        return ""

    @classmethod
    def _parse_date_from_title(cls, title):
        """Extract YYYY-MM-DD from a French section title."""
        if not title:
            return ""
        normalized = unicodedata.normalize("NFKD", title.lower())
        normalized = "".join(c for c in normalized if not unicodedata.combining(c))

        # "mars 2026", "janvier 2025", etc.
        m = re.search(
            r"\b(janvier|fevrier|mars|avril|mai|juin|juillet|aout|"
            r"septembre|octobre|novembre|decembre)\s+(20\d{2})\b",
            normalized,
        )
        if m:
            month_num = cls.MONTHS_FR.get(m.group(1))
            year = int(m.group(2))
            if month_num:
                return f"{year:04d}-{month_num:02d}-01"

        # "hiver 2025-2026", "ete 2025", etc.
        s_m = re.search(
            r"\b(hiver|printemps|ete|automne)\s+(20\d{2})(?:-(20\d{2}))?", normalized
        )
        if s_m:
            season_month = {"hiver": 12, "printemps": 3, "ete": 6, "automne": 9}
            m_num = season_month[s_m.group(1)]
            # hiver 2025-2026 → season starts Dec 2025
            year = int(s_m.group(2))
            return f"{year:04d}-{m_num:02d}-01"

        # bare year
        y_m = re.search(r"\b(20\d{2})\b", normalized)
        if y_m:
            return f"{y_m.group(1)}-01-01"

        return ""

    @staticmethod
    def _post_number(external_id, title):
        """Extract a numeric tracking key from the PDF filename (e.g. bcm202603 → '202603')."""
        if not external_id:
            return None
        m = re.search(r"(20\d{2})(\d{2})", external_id)
        if m:
            return m.group(1) + m.group(2)
        m = re.search(r"(20\d{2})", external_id)
        if m:
            return m.group(1)
        return external_id
