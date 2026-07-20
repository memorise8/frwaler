# -*- coding: utf-8 -*-
"""Crawler for english.mee.gov.cn - MEE Resources/Reports section.

Starting URL: https://english.mee.gov.cn/Resources/Reports/reports/

Two sub-sections are crawled:
  - Climate Change Annual Report  (/Resources/Reports/reports/)
  - Annual Report for Nuclear Safety  (/Resources/Reports/Annual_Report_for_Nuclear_Safety/)

Each item is a direct PDF link (no HTML detail page).
Abstracts are extracted from PDF text via pdftotext (first 10 pages),
skipping cover/ToC pages by detecting dot-leader lines.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import urljoin, urlparse

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, ".")

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_SITE_ID = "english-mee-gov-cn-resources"
_BASE_URL = "https://english.mee.gov.cn"
_PUBLISHER = "Ministry of Ecology and Environment of the People's Republic of China"
_MAX_PAGES = 200
_ABSTRACT_SAVE_MIN = 50   # per-spec: skip if <50 chars; extraction target is >=100

_SECTIONS = [
    (
        f"{_BASE_URL}/Resources/Reports/reports/",
        "Climate Change Annual Report",
    ),
    (
        f"{_BASE_URL}/Resources/Reports/Annual_Report_for_Nuclear_Safety/",
        "Annual Report for Nuclear Safety",
    ),
]


class EnglishMeeGovCnResourcesCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: english-mee-gov-cn-resources"
    base_url = _BASE_URL

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self._detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url: str, *, binary: bool = False, timeout: int = 90) -> bytes | str | None:
        """Fetch URL via curl with 3-attempt exponential backoff (1 s / 3 s / 9 s)."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--connect-timeout", "20",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: en,zh-CN;q=0.9,zh;q=0.8",
            url,
        ]
        waits = [1, 3, 9]
        last_error = "unknown"
        for attempt, wait in enumerate(waits, start=1):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 15, check=False,
                )
                if result.returncode == 0 and result.stdout:
                    if binary:
                        return result.stdout
                    return result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} stderr={stderr!r}"
            except Exception as exc:
                last_error = str(exc)

            if attempt < len(waits):
                print(
                    f"[{_SITE_ID}] curl attempt {attempt}/3 failed for {url}: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{_SITE_ID}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    # ------------------------------------------------------------------
    # PDF text extraction
    # ------------------------------------------------------------------

    def _extract_pdf_abstract(self, pdf_bytes: bytes) -> str:
        """Run pdftotext on the first 10 pages; return first substantive block.

        Skips cover and Table-of-Contents pages by detecting dot-leader lines
        (sequences of 4+ dots used as page-number leaders in ToC entries).
        Returns at most 600 chars of the first content page.  Returns "" on failure.
        """
        if not pdf_bytes:
            return ""
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
                fh.write(pdf_bytes)
                tmp_path = fh.name

            result = subprocess.run(
                ["pdftotext", "-l", "10", tmp_path, "-"],
                capture_output=True, timeout=60, check=False,
            )
            if result.returncode != 0 and not result.stdout:
                print(f"[{_SITE_ID}] pdftotext failed (rc={result.returncode})")
                return ""
            raw = result.stdout.decode("utf-8", errors="replace")
            return self._abstract_from_pdf_text(raw)
        except Exception as exc:
            print(f"[{_SITE_ID}] pdftotext error: {exc}")
            return ""
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    @staticmethod
    def _abstract_from_pdf_text(text: str) -> str:
        """Find the first content page (non-cover, non-ToC) and return a snippet.

        Pages are separated by form-feed characters.  A page is considered
        ToC if more than 30 % of its non-empty lines contain 4+ consecutive
        dots (the standard dot-leader pattern used in PDF ToC entries).
        """
        pages = text.split("\f")
        for page in pages:
            lines = [ln.strip() for ln in page.splitlines() if ln.strip()]
            if len(lines) < 3:
                continue
            toc_count = sum(1 for ln in lines if re.search(r"\.{4,}", ln))
            # Skip pages dominated by ToC entries
            if toc_count > len(lines) * 0.3:
                continue
            # Gather non-ToC lines long enough to be real sentences
            content_lines = [
                ln for ln in lines
                if not re.search(r"\.{4,}", ln) and len(ln) > 20
            ]
            if not content_lines:
                continue
            blob = " ".join(content_lines)
            if len(blob) >= 100:
                return blob[:600].strip()
        return ""

    # ------------------------------------------------------------------
    # HTML parsing helpers
    # ------------------------------------------------------------------

    def _parse_html(self, raw: str | None) -> BeautifulSoup | None:
        if not raw:
            return None
        text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[{_SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
        return None

    # ------------------------------------------------------------------
    # Listing page pagination helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _count_pages(html: str) -> int:
        """Extract total page count from the MEPC CMS JS variable ``countPage``."""
        m = re.search(r"var\s+countPage\s*=\s*(\d+)", html)
        return int(m.group(1)) if m else 1

    @staticmethod
    def _page_url(base: str, page_num: int) -> str:
        """Return the URL for listing page N (MEPC CMS: index_2.htm, index_3.htm …)."""
        if page_num == 1:
            return base
        return base.rstrip("/") + f"/index_{page_num}.htm"

    # ------------------------------------------------------------------
    # List page parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, raw: str, section_base_url: str) -> list[dict]:
        """Return item dicts from one MEPC CMS listing page."""
        soup = self._parse_html(raw)
        if soup is None:
            return []

        items: list[dict] = []
        for li in soup.select("ul#div li"):
            try:
                a = li.select_one("div.art_title a[href]")
                if not a:
                    continue
                href = (a.get("href") or "").strip()
                if not href or ".pdf" not in href.lower():
                    continue
                title = ((a.get("title") or "") or a.get_text(strip=True) or "").strip()
                if not title:
                    continue
                pdf_url = urljoin(section_base_url, href)

                date_div = li.select_one("div.date")
                date_text = date_div.get_text(strip=True) if date_div else ""

                items.append({
                    "title": title,
                    "href": href,
                    "pdf_url": pdf_url,
                    "date": date_text,
                })
            except Exception as exc:
                print(f"[{_SITE_ID}] list item parse error: {exc}")
                continue

        return items

    def _fetch_section(self, section_base_url: str, seen_urls: set) -> list[dict]:
        """Walk all pagination pages of one section; return new item dicts."""
        all_items: list[dict] = []
        section_seen: set[str] = set()

        for page_num in range(1, _MAX_PAGES + 1):
            url = self._page_url(section_base_url, page_num)
            raw = self._curl(url, timeout=45)
            if not raw:
                print(f"[{_SITE_ID}] no response for {url}; stopping section")
                break

            total_pages = self._count_pages(raw)
            items = self._parse_list_page(raw, section_base_url)

            new_items = [
                it for it in items
                if it["pdf_url"] not in seen_urls and it["pdf_url"] not in section_seen
            ]
            if not new_items and page_num > 1:
                print(f"[{_SITE_ID}] page {page_num}: 0 new items; stopping section")
                break

            for it in new_items:
                section_seen.add(it["pdf_url"])
            all_items.extend(new_items)

            if page_num == _MAX_PAGES:
                print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached for {section_base_url}")
                break
            if page_num >= total_pages:
                break

        return all_items

    # ------------------------------------------------------------------
    # Metadata helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _date_from_path(href: str) -> str:
        """Extract YYYY-MM-DD from a path segment like ./202203/P020....pdf."""
        m = re.search(r"[/\\](\d{4})(\d{2})[/\\]", href)
        if m:
            return f"{m.group(1)}-{m.group(2)}-01"
        return ""

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        saved = 0
        seen_urls: set[str] = set()
        limit_label = str(limit) if limit is not None else "∞"
        crawl_start = time.time()
        list_page = 0

        for section_url, category in _SECTIONS:
            if limit is not None and saved >= limit:
                break
            if time.time() - crawl_start > 25 * 60:
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached; stopping.")
                break

            try:
                items = self._fetch_section(section_url, seen_urls)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] section {section_url} fetch error: {exc}")
                continue

            list_page += 1
            if list_page % 10 == 0:
                print(f"[{_SITE_ID}] page {list_page}: saved {saved}/{limit_label}")

            print(f"[{_SITE_ID}] section \"{category}\": {len(items)} new items found")

            if not items:
                print(f"[{_SITE_ID}] no PDF items found in section; continuing")
                continue

            for item in items:
                if limit is not None and saved >= limit:
                    break
                if time.time() - crawl_start > 25 * 60:
                    print(f"[{_SITE_ID}] 25-minute wall-clock budget reached; stopping.")
                    break

                pdf_url = item["pdf_url"]
                if pdf_url in seen_urls:
                    continue
                seen_urls.add(pdf_url)

                try:
                    time.sleep(self._detail_delay)

                    title = item["title"]
                    href = item["href"]
                    date_raw = item.get("date", "")
                    filename = os.path.basename(urlparse(pdf_url).path)
                    external_id = os.path.splitext(filename)[0]   # e.g. "P020251105496730575050"

                    # post_number: numeric tail of the filename (up to 20 digits)
                    digits = re.sub(r"[^0-9]", "", external_id)
                    post_number = digits[-20:] if digits else external_id

                    # Prefer the formatted date from the listing; fall back to path
                    if re.match(r"\d{4}-\d{2}-\d{2}", date_raw):
                        listed_date = date_raw
                    else:
                        listed_date = self._date_from_path(href)
                    published_date = listed_date

                    print(f"[{_SITE_ID}] downloading PDF: {filename}")
                    pdf_bytes = self._curl(pdf_url, binary=True, timeout=180)
                    if not pdf_bytes:
                        print(f"[{_SITE_ID}] item {filename} failed: empty PDF response; skipping")
                        continue

                    abstract = self._extract_pdf_abstract(pdf_bytes)

                    if len(abstract) < _ABSTRACT_SAVE_MIN:
                        print(
                            f"[{_SITE_ID}] item {filename} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    metadata = {
                        "posted_date": date_raw,
                        "section": category,
                        "list_url": section_url,
                        "href": href,
                        "originalFilename": filename,
                    }

                    paper = {
                        "site_id": _SITE_ID,
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "authors": "",
                        "publisher": _PUBLISHER,
                        "department": category,
                        "journal": "",
                        "url": pdf_url,
                        "pdf_url": pdf_url,
                        "keywords": "",
                        "category": category,
                        "doi": "",
                        "original_filename": filename,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] saved [{saved}]: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {item.get('pdf_url', '?')} failed: {exc}")
                    continue

        print(f"[{_SITE_ID}] crawl complete: saved {saved}/{limit_label}")
        return saved
