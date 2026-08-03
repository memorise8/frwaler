# -*- coding: utf-8 -*-
"""Crawler for minscfa.gov.gr press releases (Γραφείο Τύπου / Δελτία Τύπου)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

# Greek month name → month number
_GREEK_MONTHS = {
    "ιανουαρίου": 1, "ιανουάριος": 1, "ιανουάριο": 1,
    "φεβρουαρίου": 2, "φεβρουάριος": 2,
    "μαρτίου": 3, "μάρτιος": 3,
    "απριλίου": 4, "απρίλιος": 4,
    "μαΐου": 5, "μάιος": 5, "μαίου": 5,
    "ιουνίου": 6, "ιούνιος": 6,
    "ιουλίου": 7, "ιούλιος": 7,
    "αυγούστου": 8, "αύγουστος": 8,
    "σεπτεμβρίου": 9, "σεπτέμβριος": 9,
    "οκτωβρίου": 10, "οκτώβριος": 10,
    "νοεμβρίου": 11, "νοέμβριος": 11,
    "δεκεμβρίου": 12, "δεκέμβριος": 12,
}


def _parse_greek_date(text: str) -> str | None:
    """Parse Greek-language date string to YYYY-MM-DD. Returns None on failure."""
    if not text:
        return None
    text = text.strip()
    # Try ISO first: 2026-05-28T...
    m = re.match(r"(\d{4}-\d{2}-\d{2})", text)
    if m:
        return m.group(1)
    # "28 Μαΐου 2026" or "28 Μαΐου, 2026"
    m = re.search(r"(\d{1,2})\s+([\w]+)[,\s]+(\d{4})", text, re.UNICODE)
    if m:
        day, month_str, year = m.group(1), m.group(2).lower(), m.group(3)
        month_num = _GREEK_MONTHS.get(month_str)
        if month_num:
            return f"{year}-{month_num:02d}-{int(day):02d}"
    return None


class MinscfaGovGrGrafeioTypouCrawler(BaseCrawler):
    site_id = "minscfa-gov-gr-grafeio-typou"
    site_name = "Custom: minscfa-gov-gr-grafeio-typou"
    base_url = "https://minscfa.gov.gr"

    START_URL = "https://minscfa.gov.gr/grafeio-typou/deltia-typou/"
    MAX_PAGES = 200
    BACKOFF = (1, 3, 9)
    CURL_TIMEOUT = 45
    MIN_ABSTRACT_CHARS = 50
    MIN_SAVED_ABSTRACT_CHARS = 100
    MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Public crawl method
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_wall = time.time()
        limit_label = str(limit) if limit is not None else "∞"

        for page in range(1, self.MAX_PAGES + 1):
            # Wall-clock guard
            elapsed = time.time() - start_wall
            if elapsed > self.MAX_WALL_SECONDS:
                print(
                    f"[{self.site_id}] wall-clock limit ({self.MAX_WALL_SECONDS}s) reached "
                    f"at page {page}; exiting cleanly"
                )
                break

            if limit is not None and saved >= limit:
                break

            list_url = self._list_url(page)
            raw = self._curl_get(list_url, context=f"list page {page}")
            if not raw:
                print(f"[{self.site_id}] list fetch failed at page {page}; stopping")
                break

            soup = self._make_soup(raw, context=f"list page {page}")
            if soup is None:
                print(f"[{self.site_id}] failed to parse list page {page}; stopping")
                break

            records = self._parse_list_page(soup)
            if not records:
                print(f"[{self.site_id}] page {page}: no records found; end of list")
                break

            if page % 10 == 0 or page == 1:
                print(
                    f"[{self.site_id}] page {page}: saved {saved}/{limit_label}"
                )

            for record in records:
                if limit is not None and saved >= limit:
                    break

                url = record.get("url", "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)

                try:
                    n = self._process_record(record)
                    if n:
                        saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {url} failed: {exc}; skipping")
                    continue

                time.sleep(self.detail_delay)

        print(f"[{self.site_id}] crawl complete: saved {saved}")
        return saved

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _list_url(self, page: int) -> str:
        if page == 1:
            return self.START_URL
        return f"{self.START_URL}page/{page}/"

    def _process_record(self, record: dict) -> int:
        """Fetch detail page, build paper dict, save. Returns 1 on success, 0 on skip."""
        url = record["url"]
        post_number = record.get("post_number")

        raw = self._curl_get(url, context=f"detail {post_number}")
        if not raw:
            print(f"[{self.site_id}] detail fetch failed for {url}; skipping")
            return 0

        soup = self._make_soup(raw, context=f"detail {url}")
        if soup is None:
            print(f"[{self.site_id}] detail parse failed for {url}; skipping")
            return 0

        # --- title ---
        title = self._get_meta(soup, "og:title") or record.get("title", "")
        # Strip site name suffix if present
        title = re.sub(r"\s*[-–|]\s*Υπουργείο.*$", "", title, flags=re.UNICODE).strip()
        if not title:
            title = record.get("title", "(untitled)")

        # --- abstract ---
        abstract = self._build_abstract(soup, record)
        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            print(
                f"[{self.site_id}] abstract too short ({len(abstract)} chars) "
                f"for {url}; skipping"
            )
            return 0
        if len(abstract) < self.MIN_SAVED_ABSTRACT_CHARS:
            print(
                f"[{self.site_id}] warning: abstract only {len(abstract)} chars for {url}"
            )

        # --- published_date ---
        iso_date = self._get_meta(soup, "article:published_time")
        published_date = _parse_greek_date(iso_date) or record.get("listed_date")

        # --- PDF ---
        pdf_url, original_filename = self._find_post_pdf(soup, url)

        # --- metadata ---
        meta = {
            "post_id": post_number,
            "modified_time": self._get_meta(soup, "article:modified_time"),
            "og_url": self._get_meta(soup, "og:url"),
        }

        self._save_paper({
            "site_id": self.site_id,
            "external_id": post_number,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "url": url,
            "published_date": published_date,
            "listed_date": record.get("listed_date"),
            "authors": None,
            "publisher": "Υπουργείο Κοινωνικής Συνοχής και Οικογένειας",
            "department": None,
            "journal": None,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "keywords": None,
            "category": "Δελτίο Τύπου",
            "doi": None,
            "metadata": json.dumps(meta, ensure_ascii=False),
        })
        return 1

    def _parse_list_page(self, soup: BeautifulSoup) -> list[dict]:
        records = []
        for article in soup.select("article.elementor-post"):
            classes = " ".join(article.get("class", []))
            pid_m = re.search(r"\bpost-(\d+)\b", classes)
            post_number = pid_m.group(1) if pid_m else None

            title_el = article.select_one(".elementor-post__title a")
            if not title_el:
                continue
            url = title_el.get("href", "").strip()
            title = title_el.get_text(separator=" ", strip=True)
            if not url:
                continue

            excerpt_el = article.select_one(".elementor-post__excerpt")
            excerpt = excerpt_el.get_text(separator=" ", strip=True) if excerpt_el else ""

            date_el = article.select_one(".elementor-post-date") or article.select_one("time")
            raw_date = date_el.get_text(strip=True) if date_el else ""
            listed_date = _parse_greek_date(raw_date)

            records.append({
                "post_number": post_number,
                "url": url,
                "title": title,
                "excerpt": excerpt,
                "listed_date": listed_date,
            })
        return records

    def _build_abstract(self, soup: BeautifulSoup, record: dict) -> str:
        # Prefer og:description (usually 200+ chars)
        og_desc = self._get_meta(soup, "og:description")
        if og_desc and len(og_desc) >= self.MIN_SAVED_ABSTRACT_CHARS:
            return og_desc.strip()

        # Fall back to entry-content paragraphs
        content = (
            soup.select_one(".entry-content")
            or soup.select_one(".elementor-widget-theme-post-content")
            or soup.select_one("article .post-content")
        )
        if content:
            paras = [p.get_text(separator=" ", strip=True) for p in content.find_all("p")]
            paras = [p for p in paras if len(p) > 20]
            combined = " ".join(paras[:6])
            if len(combined) >= self.MIN_SAVED_ABSTRACT_CHARS:
                return combined[:2000]

        # Last resort: excerpt from list page
        return record.get("excerpt", "")

    def _find_post_pdf(self, soup: BeautifulSoup, page_url: str) -> tuple[str | None, str | None]:
        """Find the first PDF link actually embedded within the post's content."""
        content = (
            soup.select_one(".entry-content")
            or soup.select_one(".elementor-widget-theme-post-content")
        )
        if not content:
            return None, None

        for a in content.find_all("a", href=True):
            href = a.get("href", "")
            if ".pdf" in href.lower():
                pdf_url = href if href.startswith("http") else urljoin(page_url, href)
                filename = urlparse(pdf_url).path.rstrip("/").split("/")[-1]
                try:
                    filename = filename.encode("latin-1").decode("utf-8")
                except Exception:
                    pass
                return pdf_url, filename or None

        return None, None

    def _get_meta(self, soup: BeautifulSoup, prop: str) -> str:
        tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        if tag:
            return (tag.get("content") or "").strip()
        return ""

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, context: str = "") -> bytes | None:
        for attempt, backoff in enumerate(self.BACKOFF):
            try:
                result = subprocess.run(
                    ["curl", "--tls-max", "1.3", "-sk", "-L",
                     "--max-time", str(self.CURL_TIMEOUT),
                     url],
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

    def _make_soup(self, raw: bytes, context: str = "") -> BeautifulSoup | None:
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                text = raw.decode("utf-8", errors="replace")
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[{self.site_id}] {parser} failed for {context}: {exc}")
        return None
