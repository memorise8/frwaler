# -*- coding: utf-8 -*-
"""Crawler for BRGM press releases (brgm.fr)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import unicodedata
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

# French month names → month number
_MONTHS_FR = {
    "janv": 1, "janvier": 1,
    "févr": 2, "fev": 2, "fevr": 2, "fevrier": 2,
    "mars": 3,
    "avr": 4, "avril": 4,
    "mai": 5,
    "juin": 6,
    "juil": 7, "juillet": 7,
    "août": 8, "aout": 8,
    "sept": 9, "septembre": 9,
    "oct": 10, "octobre": 10,
    "nov": 11, "novembre": 11,
    "déc": 12, "dec": 12, "decembre": 12,
}


class BrgmFrFrCrawler(BaseCrawler):
    site_id = "brgm-fr-fr"
    site_name = "Custom: brgm-fr-fr"
    base_url = "https://www.brgm.fr"

    START_URL = (
        "https://www.brgm.fr/fr/actualites"
        "?type=Communiqu%C3%A9%20de%20presse"
    )
    CURL_TIMEOUT = 45
    BACKOFF = (1, 3, 9)
    MIN_ABSTRACT = 50
    MAX_PAGES = 200

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self._detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Walk paginated press-release list and persist each item."""
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.monotonic()
        limit_str = str(limit) if limit is not None else "∞"

        try:
            for page in range(self.MAX_PAGES):
                elapsed = time.monotonic() - start_time
                if elapsed > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                    print(f"[{self.site_id}] 25-minute budget reached at page {page}; stopping")
                    break

                if limit is not None and saved >= limit:
                    break

                list_url = self._list_url(page)
                raw = self._curl_get(list_url, context=f"list page {page}")
                if not raw:
                    print(f"[{self.site_id}] list page {page} fetch failed; stopping")
                    break

                soup = self._make_soup(raw, context=f"list page {page}")
                if soup is None:
                    print(f"[{self.site_id}] list page {page} parse failed; stopping")
                    break

                cards = soup.select("a.bk-article[href]")
                if not cards:
                    print(f"[{self.site_id}] page {page}: no items found; stopping")
                    break

                if page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

                new_this_page = 0
                for card in cards:
                    if limit is not None and saved >= limit:
                        break

                    href = card.get("href", "").strip()
                    if not href:
                        continue
                    detail_url = urljoin(self.base_url, href)
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_this_page += 1

                    try:
                        list_meta = self._parse_card(card, detail_url)
                        time.sleep(self._detail_delay)
                        detail_raw = self._curl_get(
                            detail_url, context=f"detail {detail_url}"
                        )
                        if not detail_raw:
                            print(f"[{self.site_id}] detail fetch failed for {detail_url}; skipping")
                            continue

                        detail_soup = self._make_soup(
                            detail_raw, context=f"detail {detail_url}"
                        )
                        if detail_soup is None:
                            print(f"[{self.site_id}] detail parse failed for {detail_url}; skipping")
                            continue

                        paper = self._build_paper(detail_soup, detail_raw, list_meta, detail_url)
                        if paper is None:
                            continue

                        self._save_paper(paper)
                        saved += 1
                        print(f"[{self.site_id}] saved {saved}/{limit_str}: {paper['title'][:80]}")

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[{self.site_id}] item {detail_url} failed: {exc}")
                        continue

                # If no new URLs appeared, we've seen everything → stop
                if new_this_page == 0:
                    print(f"[{self.site_id}] page {page}: all items already seen; stopping")
                    break

                if not self._has_next_page(soup, page):
                    print(f"[{self.site_id}] page {page}: no next page; stopping")
                    break

                if page == self.MAX_PAGES - 1:
                    print(f"[{self.site_id}] safety cap of {self.MAX_PAGES} pages reached; stopping")

        except KeyboardInterrupt:
            print(f"[{self.site_id}] interrupted by user after {saved} saves")
            raise

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # List-page parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card, detail_url: str) -> dict:
        """Extract metadata available on the list card."""
        title_el = card.select_one(".article-title")
        title = self._node_text(title_el) if title_el else self._node_text(card)

        date_el = card.select_one("[class*='date'], time")
        listed_date = self._parse_date(self._node_text(date_el)) if date_el else ""

        # Category is the first chunk of card text before the title
        cat_el = card.select_one("[class*='tag'], [class*='type'], [class*='category'], [class*='theme']")
        category = self._node_text(cat_el) if cat_el else "Communiqué de presse"

        slug = urlparse(detail_url).path.rstrip("/").split("/")[-1]
        return {
            "url": detail_url,
            "external_id": slug,
            "title": title,
            "listed_date": listed_date,
            "category": category,
        }

    # ------------------------------------------------------------------
    # Detail-page parsing
    # ------------------------------------------------------------------

    def _build_paper(self, soup, raw_html: str, list_meta: dict, detail_url: str):
        # Title
        h1 = soup.find("h1")
        title = (
            self._node_text(h1)
            or self._clean_title(self._meta_content(soup, "og:title", "twitter:title"))
            or list_meta.get("title", "")
        )
        title = self._clean_title(title)
        if not title:
            print(f"[{self.site_id}] {detail_url}: no title; skipping")
            return None

        # Date – prefer detail page date element
        date_el = soup.select_one(".zt-date, [class*='zt-date']")
        if not date_el:
            # fall back to any element with 'date' class that contains digits
            for d in soup.select("[class*='date'], time"):
                txt = d.get_text(strip=True)
                if txt and any(c.isdigit() for c in txt):
                    date_el = d
                    break
        date_text = self._node_text(date_el) if date_el else list_meta.get("listed_date", "")
        published_date = self._parse_date(date_text) or list_meta.get("listed_date", "")
        listed_date = list_meta.get("listed_date", "") or published_date

        # Abstract – collect body paragraphs, fall back to og:description
        abstract = self._extract_abstract(soup)
        if not abstract:
            abstract = self._meta_content(soup, "og:description", "description", "twitter:description")
        abstract = self._clean_text(abstract)

        if len(abstract) < self.MIN_ABSTRACT:
            print(
                f"[{self.site_id}] {detail_url}: abstract too short "
                f"({len(abstract)} chars); skipping"
            )
            return None

        # Canonical URL
        canon = soup.select_one("link[rel='canonical']")
        url = (canon.get("href", "") if canon else "") or detail_url
        if not url.startswith("http"):
            url = urljoin(self.base_url, url)

        # PDF links
        pdf_url = None
        original_filename = None
        for link in soup.select("a[href]"):
            href = link.get("href", "")
            if re.search(r"\.pdf(?:[?#]|$)", href, re.I):
                pdf_url = urljoin(self.base_url, href)
                original_filename = href.rstrip("/").split("/")[-1].split("?")[0]
                break

        # Node ID from JS (best effort)
        node_id = None
        m = re.search(r'"currentPath"\s*:\s*"node/(\d+)"', raw_html or "")
        if m:
            node_id = m.group(1)
        if not node_id:
            body = soup.find("body")
            if body:
                for cls in body.get("class", []):
                    m2 = re.match(r"page-node-(\d+)$", cls)
                    if m2:
                        node_id = m2.group(1)
                        break

        slug = list_meta.get("external_id", "")
        post_number = node_id if node_id else slug

        metadata = {
            "posted_date": date_text,
            "node_id": node_id,
            "category": list_meta.get("category", ""),
            "detail_url": detail_url,
        }

        return {
            "site_id": self.site_id,
            "external_id": slug,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "url": url,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "authors": None,
            "publisher": "BRGM (Bureau de Recherches Géologiques et Minières)",
            "department": None,
            "journal": None,
            "keywords": None,
            "category": list_meta.get("category", "Communiqué de presse"),
            "doi": None,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _extract_abstract(self, soup) -> str:
        """Extract article body text from the detail page."""
        # Clone the soup so we can decompose without affecting caller
        body_copy = BeautifulSoup(str(soup), "html.parser")

        # Remove noisy elements
        for bad in body_copy.select(
            "script, style, noscript, nav, header, footer, "
            ".bk-share, .share, [class*='share'], "
            ".article-content, .related, [class*='related'], "
            ".bk-banner, .banner, .pager, form, "
            "[class*='breadcrumb'], [class*='sidebar']"
        ):
            bad.decompose()

        # The main article body lives in the page-section that contains h1
        main_section = None
        for sec in body_copy.select(".page-section, .bk-section, main"):
            if sec.find("h1"):
                main_section = sec
                break
        container = main_section or body_copy

        parts = []
        for node in container.find_all("p", recursive=True):
            txt = node.get_text(" ", strip=True)
            # Filter very short noise or navigation text
            if len(txt) < 30:
                continue
            if txt not in parts:
                parts.append(txt)

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, context: str = "request") -> str | None:
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--compressed",
            "--connect-timeout", "15",
            "--max-time", str(self.CURL_TIMEOUT),
            "-A", self.USER_AGENT,
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: fr-FR,fr;q=0.9,en;q=0.7",
            url,
        ]
        last_err = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=self.CURL_TIMEOUT + 10)
                body = (result.stdout or b"").decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                if result.returncode != 0:
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if not body.strip():
                    raise RuntimeError("empty response")
                return body
            except Exception as exc:
                last_err = str(exc)
                print(f"[{self.site_id}] {context} curl failed (attempt {attempt}/3): {last_err}")
                if attempt < 3:
                    wait = self.BACKOFF[attempt - 1]
                    print(f"[{self.site_id}] retrying in {wait}s...")
                    time.sleep(wait)
        print(f"[{self.site_id}] {context} giving up after 3 attempts: {last_err}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, raw, context: str = "HTML"):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                last_exc = exc
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        print(f"[{self.site_id}] all parsers failed for {context}: {last_exc}")
        return None

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _list_url(self, page: int) -> str:
        if page == 0:
            return self.START_URL
        return f"{self.START_URL}&page={page}"

    def _has_next_page(self, soup, page: int) -> bool:
        # Look for a pager link pointing to the next page
        next_page = page + 1
        for link in soup.select("nav.pager a[href]"):
            href = link.get("href", "")
            if f"page={next_page}" in href:
                return True
        return False

    @classmethod
    def _parse_date(cls, value: str) -> str:
        """Convert French date text to YYYY-MM-DD. Returns '' on failure."""
        text = cls._strip_accents((value or "").lower()).strip()
        if not text:
            return ""
        # ISO already
        m = re.search(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b", text)
        if m:
            return m.group(0)
        # DD mon. YYYY  or  D mon YYYY  (e.g. "12 mai 2026", "10 avr. 2026")
        m = re.search(
            r"\b(\d{1,2})\s+([a-z]+\.?)\s+((?:19|20)\d{2})\b",
            text,
        )
        if not m:
            return ""
        day = int(m.group(1))
        mon_raw = m.group(2).rstrip(".")
        year = int(m.group(3))
        month = _MONTHS_FR.get(mon_raw)
        if not month:
            return ""
        return f"{year:04d}-{month:02d}-{day:02d}"

    @staticmethod
    def _strip_accents(value: str) -> str:
        normalized = unicodedata.normalize("NFKD", value or "")
        return "".join(ch for ch in normalized if not unicodedata.combining(ch))

    @staticmethod
    def _clean_text(value: str) -> str:
        text = str(value or "")
        text = text.replace("\xa0", " ")
        text = text.replace("​", "")
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def _node_text(cls, node) -> str:
        if node is None:
            return ""
        return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()

    @classmethod
    def _clean_title(cls, title: str) -> str:
        title = re.sub(r"\s+", " ", (title or "")).strip()
        title = re.sub(r"\s*\|\s*.*$", "", title)
        return title.strip()

    @staticmethod
    def _meta_content(soup, *keys: str) -> str:
        for key in keys:
            for attr in ("name", "property"):
                node = soup.find("meta", attrs={attr: key})
                if node and node.get("content"):
                    return node.get("content", "").strip()
        return ""
