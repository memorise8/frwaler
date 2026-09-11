# -*- coding: utf-8 -*-
"""Crawler for BAW (Bundesanstalt für Wasserbau) Annual Reports.

Source: https://www.baw.de/en/publikationen/geschaeftsbericht/geschaeftsbericht.html
API:    SOAP endpoint /content/v2/publis/ws.php returns the full list in one
        response (no pagination).  Each item has a title and a direct PDF link.
        Abstracts are extracted from the foreword pages of the PDF via pdftotext.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class BawDeEnCrawler(BaseCrawler):
    """Crawl BAW Annual Report PDFs via the site's SOAP publis API."""

    site_id = "baw-de-en"
    site_name = "Custom: baw-de-en"
    base_url = "https://www.baw.de"

    _LIST_URL = (
        "https://www.baw.de/en/publikationen/geschaeftsbericht/geschaeftsbericht.html"
    )
    _SOAP_ENDPOINT = "https://www.baw.de/content/v2/publis/ws.php"
    _SOAP_SOURCE = "geschaeftsberichte"
    _SOAP_LANG = "en"

    PUBLISHER = "Bundesanstalt für Wasserbau (BAW)"
    CATEGORY = "Annual Report"

    BACKOFF_SECONDS = (1, 3, 9)
    CURL_TIMEOUT = 300          # PDFs can be ~28 MB
    PDF_TEXT_TIMEOUT = 90
    PDF_TEXT_START_PAGE = 2     # skip page 1 (org-chart cover)
    PDF_TEXT_END_PAGE = 15      # foreword is usually pp. 3–6
    MIN_ABSTRACT_CHARS = 50
    PREFERRED_ABSTRACT_CHARS = 100
    MAX_ABSTRACT_CHARS = 5000
    MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute budget per crawl run
    MAX_PAGES_SAFETY = 200      # pagination safety cap (not needed here but kept)

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        saved = 0
        seen_urls: set[str] = set()

        items = self._fetch_items()
        if not items:
            print(f"[{self.site_id}] no items found from SOAP API; aborting")
            return saved

        limit_str = str(limit) if limit is not None else "∞"
        print(f"[{self.site_id}] discovered {len(items)} items; limit={limit_str}")

        for idx, item in enumerate(items):
            if limit is not None and saved >= limit:
                break

            if time.time() - start_time > self.MAX_WALL_SECONDS:
                print(f"[{self.site_id}] wall-clock budget ({self.MAX_WALL_SECONDS}s) exceeded; stopping")
                break

            if idx > 0 and idx % 10 == 0:
                print(f"[{self.site_id}] page 1: saved {saved}/{limit_str}")

            pdf_url = item.get("pdf_url") or ""
            title = item.get("title") or ""

            if not title or not pdf_url:
                print(f"[{self.site_id}] item {idx + 1}: no title or pdf_url, skipping")
                continue

            if pdf_url in seen_urls:
                print(f"[{self.site_id}] item {idx + 1}: duplicate URL, skipping")
                continue
            seen_urls.add(pdf_url)

            try:
                year_match = re.search(r'\b(20\d{2}|19\d{2})\b', title)
                year = year_match.group(1) if year_match else None

                raw_filename = os.path.basename(urlparse(pdf_url).path)
                try:
                    filename = unquote(raw_filename)
                except Exception:
                    filename = raw_filename

                external_id = (
                    re.sub(r'\.pdf$', '', filename, flags=re.IGNORECASE)
                    if filename
                    else hashlib.sha1(pdf_url.encode()).hexdigest()[:20]
                )
                # A report year is not an exact publication date.
                published_date = None

                time.sleep(self.detail_delay)
                pdf_bytes = self._curl_get_bytes(pdf_url, context=f"item {idx + 1} PDF")
                if pdf_bytes is None:
                    print(f"[{self.site_id}] item {idx + 1} PDF fetch failed; skipping")
                    continue

                pdf_text = self._extract_pdf_text(pdf_bytes, context=f"item {idx + 1}")
                abstract = self._make_abstract(pdf_text)

                if len(abstract) < self.MIN_ABSTRACT_CHARS:
                    print(
                        f"[{self.site_id}] item {idx + 1} abstract too short "
                        f"({len(abstract)} chars); skipping"
                    )
                    continue

                metadata = {
                    "source": "BAW SOAP publis API",
                    "soap_endpoint": self._SOAP_ENDPOINT,
                    "soap_source": self._SOAP_SOURCE,
                    "year": year,
                    "originalFilename": filename,
                    "posted_date": published_date,
                }

                paper = {
                    "site_id": self.site_id,
                    "external_id": external_id,
                    "title": title,
                    "abstract": abstract,
                    "published_date": published_date,
                    "posted_date": published_date,
                    "authors": None,
                    "publisher": self.PUBLISHER,
                    "journal": None,
                    "url": pdf_url,
                    "pdf_url": pdf_url,
                    "keywords": "annual report,BAW,waterways,hydraulic engineering,Germany",
                    "category": self.CATEGORY,
                    "doi": None,
                    "original_filename": filename,
                    "metadata": json.dumps(metadata, ensure_ascii=False),
                }

                self._save_paper(paper)
                saved += 1
                print(f"[{self.site_id}] saved {saved}/{limit_str}: {title[:80]}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[baw-de-en] item {idx + 1} failed: {exc}")
                continue

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # SOAP fetch + HTML parse
    # ------------------------------------------------------------------

    def _fetch_items(self) -> list[dict]:
        soap_body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<soap:Envelope'
            ' xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"'
            ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
            ' xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
            '<soap:Body>'
            '<getPublis>'
            f'<language>{self._SOAP_LANG}</language>'
            f'<source>{self._SOAP_SOURCE}</source>'
            f'<url>{self._LIST_URL}</url>'
            '</getPublis>'
            '</soap:Body>'
            '</soap:Envelope>'
        )

        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--connect-timeout", "20",
            "--max-time", "60",
            "-A", self.USER_AGENT,
            "-H", "Content-Type: text/xml; charset=utf-8",
            "-H", 'SOAPAction: ""',
            "--data", soap_body,
            self._SOAP_ENDPOINT,
        ]

        raw = None
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=70
                )
                if result.returncode != 0:
                    err = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                    raise RuntimeError(err or f"curl exit {result.returncode}")
                raw = result.stdout
                if raw:
                    break
            except Exception as exc:
                print(f"[{self.site_id}] SOAP fetch attempt {attempt}/3 failed: {exc}")
                if attempt < 3:
                    wait = self.BACKOFF_SECONDS[attempt - 1]
                    time.sleep(wait)

        if not raw:
            return []

        text = raw.decode("utf-8", errors="replace")
        # SOAP response wraps HTML inside a <body> tag; it's HTML-entity-escaped
        body_match = re.search(r'<body[^>]*xsi:type[^>]*>(.*?)</body>', text, re.DOTALL)
        if not body_match:
            # Try simpler match
            body_match = re.search(r'<body[^>]*>(.*?)</body>', text, re.DOTALL)
        if not body_match:
            print(f"[{self.site_id}] could not find <body> in SOAP response")
            return []

        html_content = unescape(body_match.group(1))
        return self._parse_html_list(html_content)

    def _parse_html_list(self, html: str) -> list[dict]:
        soup = self._make_soup(html, context="SOAP HTML body")
        if soup is None:
            return []

        items = []
        for li in soup.select("li"):
            h4 = li.find("h4")
            if not h4:
                continue
            title = h4.get_text(" ", strip=True)
            title = re.sub(r'\s+', ' ', unescape(title)).strip()
            if not title:
                continue

            download_link = li.find("a", class_="download")
            if not download_link:
                continue
            pdf_url = (download_link.get("href") or "").strip()
            if not pdf_url:
                continue
            if not pdf_url.startswith("http"):
                pdf_url = urljoin(self.base_url, pdf_url)

            items.append({"title": title, "pdf_url": pdf_url})

        return items

    # ------------------------------------------------------------------
    # PDF download and text extraction
    # ------------------------------------------------------------------

    def _curl_get_bytes(self, url: str, context: str = "request") -> bytes | None:
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--connect-timeout", "20",
            "--max-time", str(self.CURL_TIMEOUT),
            "-A", self.USER_AGENT,
            "-H", "Accept: application/pdf,*/*;q=0.8",
            url,
        ]
        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=self.CURL_TIMEOUT + 10
                )
                if result.returncode != 0:
                    err = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                    raise RuntimeError(err or f"curl exit {result.returncode}")
                body = result.stdout
                if not body:
                    raise RuntimeError("empty response")
                return body
            except Exception as exc:
                last_error = str(exc)
                print(f"[{self.site_id}] {context} curl attempt {attempt}/3 failed: {last_error}")
                if attempt < 3:
                    wait = self.BACKOFF_SECONDS[attempt - 1]
                    print(f"[{self.site_id}] retrying in {wait}s...")
                    time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None

    def _extract_pdf_text(self, pdf_bytes: bytes, context: str = "PDF") -> str:
        cmd = [
            "pdftotext",
            "-f", str(self.PDF_TEXT_START_PAGE),
            "-l", str(self.PDF_TEXT_END_PAGE),
            "-enc", "UTF-8",
            "-",   # stdin
            "-",   # stdout
        ]
        try:
            result = subprocess.run(
                cmd,
                input=pdf_bytes,
                capture_output=True,
                timeout=self.PDF_TEXT_TIMEOUT,
            )
        except FileNotFoundError:
            print(f"[{self.site_id}] pdftotext not available for {context}")
            return ""
        except Exception as exc:
            print(f"[{self.site_id}] pdftotext failed for {context}: {exc}")
            return ""

        if result.returncode != 0:
            err = (result.stderr or b"").decode("utf-8", errors="replace").strip()
            print(f"[{self.site_id}] pdftotext non-zero for {context}: {err}")

        return (result.stdout or b"").decode("utf-8", errors="replace")

    def _make_abstract(self, text: str) -> str:
        """Turn raw pdftotext output into a clean abstract string."""
        if not text:
            return ""

        text = text.replace("\x0c", "\n")  # page-feed → newline

        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            # Skip very short lines — org-chart labels, page numbers, counters
            if len(stripped) < 25:
                continue
            # Skip lines that are purely numeric / symbolic (stat tables)
            if re.match(r'^[\d\s.,+\-:/%€·×]+$', stripped):
                continue
            # Stop at table-of-contents marker ("Inhalt")
            if re.match(r'^Inhalt\s*$', stripped, re.IGNORECASE):
                break
            lines.append(stripped)

        joined = ' '.join(lines)
        joined = re.sub(r'\s+', ' ', joined).strip()

        if len(joined) > self.MAX_ABSTRACT_CHARS:
            joined = joined[:self.MAX_ABSTRACT_CHARS].rsplit(' ', 1)[0].rstrip('.,; ') + '.'

        return joined

    # ------------------------------------------------------------------
    # HTML parsing helper
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
