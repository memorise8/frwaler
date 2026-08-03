# -*- coding: utf-8 -*-
"""Higher Education Authority (HEA Ireland) – Publications crawler.

List:   GET https://hea.ie/resources/publications/page/{N}/
        (page 1 = https://hea.ie/resources/publications/)
Detail: Not visited — all metadata is on list page; abstract extracted from PDF.
"""

from __future__ import annotations

import io
import json
import os
import re
import time
import unicodedata
from html import unescape
from typing import Optional
from urllib.parse import urlparse

from crawler.base_crawler import BaseCrawler

try:
    import pypdf
    _HAS_PYPDF = True
except ImportError:
    _HAS_PYPDF = False

try:
    import pdfplumber
    _HAS_PDFPLUMBER = True
except ImportError:
    _HAS_PDFPLUMBER = False


def _make_soup(html: str):
    """Return BeautifulSoup with fallback parsers; None on total failure."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _title_to_slug(title: str) -> str:
    """WordPress-style title → URL slug."""
    title = unicodedata.normalize("NFKD", title)
    title = title.encode("ascii", "ignore").decode("ascii")
    title = title.lower()
    title = re.sub(r"[^\w\s-]", "", title)
    title = re.sub(r"[\s_]+", "-", title.strip())
    title = re.sub(r"-+", "-", title)
    return title.strip("-")


def _pdf_filename(url: str) -> str:
    """Extract filename from PDF URL path."""
    path = urlparse(url).path
    return path.rstrip("/").split("/")[-1]


class HeaIeResourcesCrawler(BaseCrawler):
    site_id = "hea-ie-resources"
    site_name = "Custom: hea-ie-resources"
    base_url = "https://hea.ie"

    _LIST_BASE = "https://hea.ie/resources/publications"
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _WALL_MINS = 25
    _MIN_ABS = 100     # skip items whose PDF yields < this many chars
    _BACKOFF = (1, 3, 9)

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _get_html(self, url: str) -> Optional[str]:
        """GET text with exponential-backoff retry; returns body or None."""
        for i, wait in enumerate((*self._BACKOFF, None)):
            try:
                resp = self._session.get(
                    url, timeout=30,
                    headers={"Accept-Encoding": "gzip, deflate"},
                )
                if resp.status_code == 200:
                    try:
                        return resp.content.decode("utf-8")
                    except UnicodeDecodeError:
                        return resp.content.decode("utf-8", errors="replace")
                print(f"[{self.site_id}] HTTP {resp.status_code} for {url}")
            except Exception as exc:
                print(f"[{self.site_id}] network error ({url}): {exc}")
            if wait is None:
                break
            time.sleep(wait)
        return None

    def _get_bytes(self, url: str) -> Optional[bytes]:
        """GET binary (PDF) with retry; returns bytes or None."""
        for i, wait in enumerate((*self._BACKOFF, None)):
            try:
                resp = self._session.get(
                    url, timeout=60,
                    headers={"Accept-Encoding": "identity"},
                )
                if resp.status_code == 200:
                    return resp.content
                print(f"[{self.site_id}] PDF HTTP {resp.status_code} for {url}")
            except Exception as exc:
                print(f"[{self.site_id}] PDF fetch error ({url}): {exc}")
            if wait is None:
                break
            time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # PDF text extraction
    # ------------------------------------------------------------------

    def _extract_pdf_text(self, data: bytes, max_chars: int = 3000) -> str:
        """Extract plain text from PDF bytes; returns '' on failure."""
        if _HAS_PYPDF:
            try:
                reader = pypdf.PdfReader(io.BytesIO(data))
                text = ""
                for page in reader.pages:
                    text += (page.extract_text() or "") + "\n"
                    if len(text) >= max_chars:
                        break
                text = re.sub(r"\s+", " ", text).strip()
                if len(text) >= 50:
                    return text[:max_chars]
            except Exception as exc:
                print(f"[{self.site_id}] pypdf error: {exc}")

        if _HAS_PDFPLUMBER:
            try:
                with pdfplumber.open(io.BytesIO(data)) as pdf:
                    text = ""
                    for page in pdf.pages:
                        text += (page.extract_text() or "") + "\n"
                        if len(text) >= max_chars:
                            break
                text = re.sub(r"\s+", " ", text).strip()
                if len(text) >= 50:
                    return text[:max_chars]
            except Exception as exc:
                print(f"[{self.site_id}] pdfplumber error: {exc}")

        return ""

    # ------------------------------------------------------------------
    # List page parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, html: str) -> list:
        """Return list of raw item dicts from one publications list page."""
        items = []
        soup = _make_soup(html)
        if soup is None:
            return items

        cards = soup.find("div", class_="publications_cards")
        if not cards:
            return items

        for article in cards.find_all("article"):
            try:
                time_tag = article.find("time", class_="date")
                strong = article.find("strong")
                lozenge = article.find("p", class_="lozenge")
                cats_dl = article.find("dl", class_="cats")

                title = strong.get_text(" ", strip=True) if strong else ""
                title = re.sub(r"\s+", " ", title).strip()
                if not title:
                    continue

                date_iso = time_tag.get("datetime", "") if time_tag else ""

                pdf_url = ""
                if lozenge:
                    a_tag = lozenge.find("a")
                    if a_tag:
                        pdf_url = a_tag.get("href", "")

                cats = []
                if cats_dl:
                    for dd in cats_dl.find_all("dd"):
                        a_tag = dd.find("a")
                        if a_tag:
                            cats.append(a_tag.get_text(strip=True))

                items.append({
                    "title": title,
                    "date": date_iso,
                    "pdf_url": pdf_url,
                    "categories": cats,
                })
            except Exception as exc:
                print(f"[{self.site_id}] article parse error: {exc}")
                continue

        return items

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        wall_deadline = time.time() + self._WALL_MINS * 60
        limit_val = limit if limit is not None else float("inf")
        saved = 0
        seen_urls: set = set()
        page = 1

        while saved < limit_val and page <= self._MAX_PAGES:
            if time.time() > wall_deadline:
                print(f"[{self.site_id}] wall-clock budget reached, stopping")
                break

            if page == 1:
                list_url = f"{self._LIST_BASE}/"
            else:
                list_url = f"{self._LIST_BASE}/page/{page}/"

            html = self._get_html(list_url)
            if not html:
                print(f"[{self.site_id}] failed to fetch page {page}, stopping")
                break

            items = self._parse_list_page(html)
            if not items:
                print(f"[{self.site_id}] page {page}: no items found, end of results")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit if limit is not None else 'inf'}")

            new_on_page = 0
            for item in items:
                if saved >= limit_val:
                    break

                pdf_url = item.get("pdf_url", "")
                title = item.get("title", "")
                dedup_key = pdf_url or title
                if dedup_key in seen_urls:
                    continue
                seen_urls.add(dedup_key)
                new_on_page += 1

                try:
                    # Fetch and extract PDF text as abstract
                    abstract = ""
                    if pdf_url:
                        pdf_data = self._get_bytes(pdf_url)
                        if pdf_data:
                            abstract = self._extract_pdf_text(pdf_data)
                        time.sleep(self._delay)

                    if len(abstract) < self._MIN_ABS:
                        print(
                            f"[{self.site_id}] skip '{title[:60]}': "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    # external_id: PDF filename without extension
                    if pdf_url:
                        fname = _pdf_filename(pdf_url)
                        ext_id = re.sub(r"\.[Pp][Dd][Ff]$", "", fname)
                    else:
                        ext_id = _title_to_slug(title)[:100]

                    # detail page URL derived from title slug (WP standard)
                    detail_slug = _title_to_slug(title)
                    detail_url = f"{self._LIST_BASE}/{detail_slug}/"

                    date_str = item.get("date", "")
                    cats = item.get("categories", [])
                    category = ", ".join(cats) if cats else None
                    original_filename = _pdf_filename(pdf_url) if pdf_url else None

                    metadata = {
                        "posted_date": date_str,
                        "originalFilename": original_filename,
                        "categories_raw": cats,
                    }

                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": ext_id,
                        "post_number": ext_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": date_str,
                        "listed_date": date_str,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "publisher": "Higher Education Authority (HEA)",
                        "category": category,
                        "keywords": category,
                        "original_filename": original_filename,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    })
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item '{title[:60]}' failed: {exc}")
                    continue

            # No new items means we've looped back or exhausted all pages
            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: all items already seen, stopping")
                break

            page += 1

        if page > self._MAX_PAGES:
            print(f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached")

        return saved
