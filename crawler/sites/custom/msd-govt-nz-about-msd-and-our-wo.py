# -*- coding: utf-8 -*-
"""Crawler for MSD New Zealand Regulatory Impact Statements.

Target: https://www.msd.govt.nz/about-msd-and-our-work/publications-resources/
        regulatory-impact-statements/index.html

Single listing page, no API — items organised by year in <div class="block">
sections. Each item is <div class="listing"> with a title link, optional
author, and year. Links go to either an HTML detail page or a direct
PDF/DOCX file.

Strategy:
  • Parse all items from the single listing page.
  • For HTML detail pages: fetch → extract abstract from <div id="content">
    paragraphs → find PDF download link.
  • For direct PDF/DOCX items: skip (no abstract can be obtained).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urljoin, urlparse

# ── absolute import: spec_from_file_location has no package context ──────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from crawler.base_crawler import BaseCrawler  # noqa: E402

# ── BeautifulSoup import (optional but required for robust HTML parsing) ──────
try:
    from bs4 import BeautifulSoup as _BS4
    _PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    _BS4 = None
    _PARSERS = []


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_soup(html: str | bytes):
    """Parse HTML with BeautifulSoup, falling back through parsers gracefully."""
    if _BS4 is None:
        return None
    for parser in _PARSERS:
        try:
            return _BS4(html, parser)
        except Exception:
            continue
    return None


def _strip_html(s: str) -> str:
    """Strip HTML tags and normalise whitespace."""
    import html as _html
    text = re.sub(r"<[^>]+>", " ", str(s))
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


# ── crawler ───────────────────────────────────────────────────────────────────

class MSDGovtNzRisCrawler(BaseCrawler):
    """Crawler for MSD New Zealand Regulatory Impact Statements."""

    site_id   = "msd-govt-nz-about-msd-and-our-wo"
    site_name = "Custom: msd-govt-nz-about-msd-and-our-wo"
    base_url  = "https://www.msd.govt.nz"

    _LIST_URL       = ("https://www.msd.govt.nz/about-msd-and-our-work/"
                       "publications-resources/regulatory-impact-statements/index.html")
    _PUBLISHER      = "Ministry of Social Development"
    _WALL_CLOCK_MAX = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))   # 25 minutes
    _MIN_ABSTRACT   = 100       # chars — items below this are skipped (test requires >=100)

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, max_retries: int = 3) -> bytes | None:
        """Fetch *url* with curl; retry with exponential back-off on failure."""
        delays = [1, 3, 9]
        for attempt in range(max_retries):
            try:
                result = subprocess.run(
                    [
                        "curl", "--tls-max", "1.3", "-sk",
                        "--max-time", "30",
                        "-H", f"User-Agent: {self.USER_AGENT}",
                        url,
                    ],
                    capture_output=True,
                    timeout=35,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt+1}/{max_retries}): {exc}")
            if attempt < max_retries - 1:
                wait = delays[attempt]
                print(f"[{self.site_id}] Retrying in {wait}s…")
                time.sleep(wait)
        return None

    def _fetch_html(self, url: str) -> str | None:
        """Fetch *url* and return decoded HTML text, or None on failure."""
        raw = self._curl_get(url)
        if raw is None:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("utf-8", errors="replace")

    # ------------------------------------------------------------------
    # Listing-page parser
    # ------------------------------------------------------------------

    def _parse_listing(self, html: str) -> list[dict]:
        """Return a list of item dicts from the RIS listing page HTML."""
        items: list[dict] = []

        soup = _make_soup(html)
        if soup is None:
            # Fallback: regex-based extraction
            return self._parse_listing_regex(html)

        for block in soup.find_all("div", class_="block"):
            h2 = block.find("h2")
            year = h2.get_text(strip=True) if h2 else ""

            for listing in block.find_all("div", class_="listing"):
                title_div = listing.find("div", class_="title")
                if not title_div:
                    continue
                a_tag = title_div.find("a", href=True)
                if not a_tag:
                    continue

                raw_title = a_tag.get_text(" ", strip=True)
                # Strip file-size annotations like "(PDF 3.08MB)"
                title = re.sub(
                    r"\s*\((?:PDF|Word|Excel|DOCX?)[^)]*\)\s*$",
                    "", raw_title, flags=re.IGNORECASE
                ).strip()
                if not title:
                    title = raw_title.strip()

                href = a_tag.get("href", "").strip()
                if not href or href == "#":
                    continue

                if href.startswith("http"):
                    url = href
                elif href.startswith("/"):
                    url = self.base_url + href
                else:
                    url = urljoin(self._LIST_URL, href)

                author_div = listing.find("div", class_="author")
                author = author_div.get_text(strip=True) if author_div else ""

                date_div = listing.find("div", class_="date")
                date_text = date_div.get_text(strip=True) if date_div else year

                lower_url = url.split("?")[0].lower()
                items.append({
                    "title":   title,
                    "url":     url,
                    "author":  author,
                    "year":    date_text or year,
                    "is_html": lower_url.endswith(".html"),
                    "is_pdf":  lower_url.endswith(".pdf"),
                    "is_docx": lower_url.endswith((".docx", ".doc")),
                })

        return items

    def _parse_listing_regex(self, html: str) -> list[dict]:
        """Fallback regex-based listing parser (used when BS4 unavailable)."""
        items: list[dict] = []
        # Find year blocks: <h2>YEAR</h2> ... items ...
        for block_m in re.finditer(
            r'<h2>(.*?)</h2>(.*?)(?=<h2>|</div>\s*</div>\s*</div>)',
            html, re.DOTALL
        ):
            year = _strip_html(block_m.group(1))
            block_html = block_m.group(2)
            for m in re.finditer(
                r'<div class="title"><a href="([^"]+)"[^>]*>(.*?)</a>',
                block_html, re.DOTALL
            ):
                href = m.group(1).strip()
                raw_title = _strip_html(m.group(2))
                title = re.sub(
                    r"\s*\((?:PDF|Word|Excel)[^)]*\)\s*$",
                    "", raw_title, flags=re.IGNORECASE
                ).strip()

                if href.startswith("http"):
                    url = href
                elif href.startswith("/"):
                    url = self.base_url + href
                else:
                    url = urljoin(self._LIST_URL, href)

                lower_url = url.split("?")[0].lower()
                items.append({
                    "title":   title or raw_title,
                    "url":     url,
                    "author":  "",
                    "year":    year,
                    "is_html": lower_url.endswith(".html"),
                    "is_pdf":  lower_url.endswith(".pdf"),
                    "is_docx": lower_url.endswith((".docx", ".doc")),
                })
        return items

    # ------------------------------------------------------------------
    # Detail-page helpers
    # ------------------------------------------------------------------

    def _extract_abstract(self, html: str) -> str:
        """Extract the main body text from an RIS detail page."""
        soup = _make_soup(html)
        if soup is None:
            return self._extract_abstract_regex(html)

        # The content lives in <div id="content"> (separate from the left nav)
        content_div = soup.find(id="content")
        if not content_div:
            content_div = soup.find("main") or soup

        # Remove the side-nav div so we don't pick up nav text
        for nav in content_div.find_all(id="nav"):
            nav.decompose()
        for nav in content_div.find_all("div", class_="page-navigation"):
            nav.decompose()

        parts: list[str] = []

        # Prefer paragraphs inside .block divs (main content area)
        for block in content_div.find_all("div", class_="block"):
            for p in block.find_all("p"):
                text = p.get_text(" ", strip=True)
                if text and "Print this page" not in text:
                    parts.append(text)

        if not parts:
            # Fallback: all <p> tags in content area
            for p in content_div.find_all("p"):
                text = p.get_text(" ", strip=True)
                if text and len(text) > 20 and "Print this page" not in text:
                    parts.append(text)

        return "\n\n".join(parts)

    def _extract_abstract_regex(self, html: str) -> str:
        """Fallback regex abstract extractor."""
        # Find content between <div id="content"> and first <!-- End of content
        m = re.search(r'id="content">(.*?)(?:<!-- End of content|<div id="feature)', html, re.DOTALL)
        region = m.group(1) if m else html
        parts = []
        for pm in re.finditer(r"<p[^>]*>(.*?)</p>", region, re.DOTALL | re.IGNORECASE):
            text = _strip_html(pm.group(1))
            if text and len(text) > 20 and "Print this page" not in text:
                parts.append(text)
        return "\n\n".join(parts)

    def _find_pdf_url(self, html: str, page_url: str) -> str | None:
        """Find the primary PDF download link in a detail page."""
        soup = _make_soup(html)
        if soup:
            content_div = soup.find(id="content") or soup
            for a in content_div.find_all("a", href=True):
                href = a["href"].strip()
                if href.lower().split("?")[0].endswith(".pdf"):
                    if href.startswith("/"):
                        return self.base_url + href
                    elif href.startswith("http"):
                        return href
            return None

        # Regex fallback
        for m in re.finditer(r'href="([^"]*\.pdf)"', html, re.IGNORECASE):
            href = m.group(1)
            if href.startswith("/"):
                return self.base_url + href
            elif href.startswith("http"):
                return href
        return None

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        """Crawl the RIS listing page and each HTML detail page.

        Parameters
        ----------
        limit : int | None
            Maximum number of records to save. None = unlimited.
        """
        start_time = time.time()
        saved = 0
        seen_urls: set[str] = set()
        limit_str = str(limit) if limit is not None else "inf"

        # ── fetch listing page ────────────────────────────────────────
        print(f"[{self.site_id}] Fetching listing page…")
        list_html = self._fetch_html(self._LIST_URL)
        if not list_html:
            print(f"[{self.site_id}] Failed to fetch listing page. Aborting.")
            return saved

        items = self._parse_listing(list_html)
        if not items:
            print(f"[{self.site_id}] No items found on listing page. Aborting.")
            return saved

        print(f"[{self.site_id}] Found {len(items)} items on listing page.")

        # ── single "page" loop (this site has only one listing page) ──
        # The structure mirrors the pagination contract: we walk items
        # until (a) saved >= limit, (b) no items left, or (c) wall clock.
        page_num = 1
        page_saved = 0

        for idx, item in enumerate(items, start=1):
            # Wall-clock budget
            elapsed = time.time() - start_time
            if elapsed > self._WALL_CLOCK_MAX:
                print(f"[{self.site_id}] Wall-clock budget reached "
                      f"({elapsed:.0f}s). Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            url = item["url"]

            if url in seen_urls:
                print(f"[{self.site_id}] Duplicate URL skipped: {url[:80]}")
                continue
            seen_urls.add(url)

            title = item["title"]

            # ── skip direct file links — no abstract obtainable ──────
            if item["is_pdf"] or item["is_docx"]:
                print(f"[{self.site_id}] Skipping direct file (no abstract): "
                      f"{title[:60]}")
                continue

            if not item["is_html"]:
                print(f"[{self.site_id}] Skipping unknown type: {url[:80]}")
                continue

            # ── per-item fetch + parse (fully isolated) ───────────────
            try:
                time.sleep(self._delay)

                detail_html = self._fetch_html(url)
                if not detail_html:
                    print(f"[{self.site_id}] item {idx} fetch failed: "
                          f"{url[:80]}")
                    continue

                abstract = self._extract_abstract(detail_html)
                if len(abstract) < self._MIN_ABSTRACT:
                    print(f"[{self.site_id}] Skipping short abstract "
                          f"({len(abstract)} chars): {title[:60]}")
                    continue

                pdf_url = self._find_pdf_url(detail_html, url)

                # Build external_id from URL slug
                path_slug = urlparse(url).path.rstrip("/")
                external_id = path_slug.split("/")[-1].replace(".html", "")
                if not external_id:
                    external_id = re.sub(r"[^a-zA-Z0-9_-]", "-", title)[:80]

                year = item["year"]
                # Validate year looks like YYYY
                pub_date = year if re.match(r"^\d{4}$", year) else None

                author = item.get("author", "").strip()
                authors_field = author if author else self._PUBLISHER

                original_filename: str | None = None
                if pdf_url:
                    fn = urlparse(pdf_url).path.split("/")[-1]
                    if fn and "." in fn:
                        original_filename = fn

                paper: dict = {
                    "site_id":           self.site_id,
                    "external_id":       external_id,
                    "title":             title,
                    "abstract":          abstract,
                    "published_date":    pub_date,
                    "posted_date":       pub_date,
                    "url":               url,
                    "pdf_url":           pdf_url,
                    "authors":           authors_field,
                    "publisher":         self._PUBLISHER,
                    "department":        None,
                    "keywords":          None,
                    "doi":               None,
                    "original_filename": original_filename,
                    "category":          _categorise(title),
                    "metadata": json.dumps({
                        "posted_date":      year,
                        "originalFilename": original_filename,
                        "year":             year,
                        "listing_url":      self._LIST_URL,
                        "item_index":       idx,
                    }, ensure_ascii=False),
                }

                self._save_paper(paper)
                saved += 1
                page_saved += 1
                print(f"[{self.site_id}] Saved {saved}/{limit_str}: "
                      f"{title[:60]}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {idx} failed: {exc}")
                continue

        # ── progress report (single page sites still log at page boundary) ──
        print(f"[{self.site_id}] page {page_num}: saved {saved}/{limit_str}")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved


def _categorise(title: str) -> str:
    tl = title.lower()
    if "supplementary analysis report" in tl:
        return "Supplementary Analysis Report"
    if "regulatory impact statement" in tl:
        return "Regulatory Impact Statement"
    return "Regulatory Impact Statement"
