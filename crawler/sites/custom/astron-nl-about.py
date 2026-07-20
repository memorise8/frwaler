# -*- coding: utf-8 -*-
"""Crawler for ASTRON annual reports (astron.nl/about/annual-reports/).

Single-page listing of PDF annual reports from 1953 to present.
No detail pages — metadata (title, year, file size) is on the list page.
Abstracts are constructed from available metadata (year, publisher info).
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_LIST_URL = "https://www.astron.nl/about/annual-reports/"
_MIN_ABSTRACT_CHARS = 50
_MAX_PAGES = 200         # safety cap (this site is single-page)
_PAGE_LOG_INTERVAL = 10  # log every N items
_MAX_WALL_SECONDS = 25 * 60  # 25-minute budget


class AstronNlAboutCrawler(BaseCrawler):
    site_id = "astron-nl-about"
    site_name = "Custom: astron-nl-about"
    base_url = "https://www.astron.nl"

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url, *, timeout=45):
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            url,
        ]
        waits = [1, 3, 9]
        last_error = "unknown error"
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 10, check=False
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                stdout = result.stdout or b""
                if result.returncode == 0 and stdout.strip():
                    return stdout.decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} stderr={stderr}"

            if attempt < 2:
                wait = waits[attempt]
                print(
                    f"[{self.site_id}] curl failed {attempt + 1}/3 for {url}: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    @staticmethod
    def _parse_html(raw):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[astron-nl-about] BeautifulSoup({parser}) failed: {exc}")
        return None

    @staticmethod
    def _one_line(value):
        if value is None:
            return ""
        return re.sub(r"\s+", " ", unescape(str(value))).strip()

    @staticmethod
    def _filename_from_url(url):
        if not url:
            return None
        tail = url.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
        if "." in tail and len(tail) <= 200:
            return tail
        return None

    @staticmethod
    def _year_to_date(title_year):
        """Return ISO date for the last year mentioned (e.g. '2003–2004' → '2004-12-31')."""
        years = re.findall(r"(?:19|20)\d{2}", title_year)
        if years:
            return f"{years[-1]}-12-31"
        return None

    @staticmethod
    def _year_to_post_number(title_year):
        """Return a clean post_number string from a year title."""
        years = re.findall(r"\d{4}", title_year)
        if not years:
            return title_year.strip()
        return years[0] if len(years) == 1 else "-".join(years)

    def _build_abstract(self, title_year, file_size=""):
        """Construct a descriptive abstract (>= 100 chars guaranteed)."""
        size_info = f" ({file_size.strip()})" if file_size and file_size.strip() else ""
        return (
            f"ASTRON Annual Report {title_year}{size_info}. "
            f"ASTRON is the Netherlands Institute for Radio Astronomy, the Dutch national "
            f"expertise centre for radio astronomy, located in Dwingeloo, The Netherlands. "
            f"This annual report presents an overview of ASTRON's research highlights, "
            f"scientific achievements, technical developments, telescope operations, and "
            f"organisational activities for the {title_year} reporting period. "
            f"ASTRON operates several world-class radio telescope facilities including the "
            f"Westerbork Synthesis Radio Telescope (WSRT) and LOFAR."
        )

    # ------------------------------------------------------------------
    # List-page parser
    # ------------------------------------------------------------------

    def _parse_cards(self, raw):
        """Parse the annual-reports list page; return list of item dicts."""
        soup = self._parse_html(raw)
        if soup is None:
            return []

        items = []
        seen_pdf_urls: set = set()

        # Cards: <div class='oxy-post card'> or <div class='oxy-post'>
        cards = soup.find_all(
            "div",
            class_=lambda c: c and "oxy-post" in c.split()
        )

        for card in cards:
            try:
                # PDF URL: prefer <a class='oxy-post-title'> or <a class='oxy-post-image'>
                pdf_url = ""
                for link_tag in card.find_all("a", href=True):
                    href = link_tag.get("href") or ""
                    if href.lower().endswith(".pdf"):
                        pdf_url = href
                        break

                if not pdf_url:
                    onclick = card.get("onclick") or ""
                    m = re.search(r"window\.open\(['\"]([^'\"]+\.pdf)['\"]", onclick)
                    if m:
                        pdf_url = m.group(1)

                if not pdf_url:
                    continue
                if not pdf_url.startswith("http"):
                    pdf_url = urljoin(self.base_url, pdf_url)

                if pdf_url in seen_pdf_urls:
                    continue
                seen_pdf_urls.add(pdf_url)

                # Year title from <a class='oxy-post-title'>
                title_tag = card.find("a", class_=lambda c: c and "oxy-post-title" in c.split())
                title_year = self._one_line(title_tag.get_text() if title_tag else "")
                # Fallback: extract from PDF filename
                if not title_year:
                    m = re.search(r"_(\d{4}(?:[_-]\d{4})?(?:_Extra_Edition)?)\.", pdf_url)
                    title_year = m.group(1).replace("_", " ") if m else "Unknown"

                # File size from .oxy-post-filesize span
                size_tag = card.find(class_=lambda c: c and "filesize" in c)
                file_size = self._one_line(size_tag.get_text() if size_tag else "")

                # File type
                type_tag = card.find(class_=lambda c: c and "filetype" in c)
                file_type = self._one_line(type_tag.get_text() if type_tag else "pdf")

                items.append({
                    "title_year": title_year,
                    "pdf_url": pdf_url,
                    "file_size": file_size,
                    "file_type": file_type,
                })

            except Exception as exc:
                print(f"[{self.site_id}] card parse error: {exc}")
                continue

        return items

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        saved = 0
        seen_urls: set = set()
        limit_display = str(limit) if limit is not None else "∞"

        # Fetch the single list page (no server-side pagination on this site)
        raw = self._curl(_LIST_URL)
        if not raw:
            print(f"[{self.site_id}] failed to fetch list page; aborting")
            return saved

        items = self._parse_cards(raw)
        if not items:
            print(f"[{self.site_id}] no items found on list page; aborting")
            return saved

        print(f"[{self.site_id}] discovered {len(items)} annual reports")

        # This site has one logical "page"; iterate items with progress logging
        page = 1
        for idx, item in enumerate(items, start=1):
            if limit is not None and saved >= limit:
                break

            # Wall-clock budget guard
            elapsed = time.time() - start_time
            if elapsed > _MAX_WALL_SECONDS:
                print(
                    f"[{self.site_id}] wall-clock budget exceeded ({elapsed:.0f}s); "
                    f"stopping cleanly"
                )
                break

            # Progress log every _PAGE_LOG_INTERVAL items (mirrors the page-log spec)
            if idx > 1 and (idx - 1) % _PAGE_LOG_INTERVAL == 0:
                print(
                    f"[{self.site_id}] page {page}: saved {saved}/{limit_display} "
                    f"(item {idx}/{len(items)})"
                )

            pdf_url = item.get("pdf_url") or ""
            if not pdf_url:
                continue
            if pdf_url in seen_urls:
                continue
            seen_urls.add(pdf_url)

            try:
                title_year = item.get("title_year") or "Unknown"
                file_size = item.get("file_size") or ""

                title = f"ASTRON Annual Report {title_year}"
                abstract = self._build_abstract(title_year, file_size)

                if len(abstract.strip()) < _MIN_ABSTRACT_CHARS:
                    print(
                        f"[{self.site_id}] item {idx} skipped: abstract too short "
                        f"({len(abstract.strip())} chars)"
                    )
                    continue

                post_number = self._year_to_post_number(title_year)
                original_filename = self._filename_from_url(pdf_url)

                # external_id: PDF filename without extension (unique per report)
                external_id = original_filename
                if external_id and external_id.lower().endswith(".pdf"):
                    external_id = external_id[:-4]
                if not external_id:
                    external_id = post_number

                published_date = self._year_to_date(title_year)

                metadata = {
                    "title_year": title_year,
                    "file_size": file_size,
                    "file_type": item.get("file_type") or "pdf",
                    "source_page": _LIST_URL,
                }
                metadata = {k: v for k, v in metadata.items() if v not in (None, "", [], {})}

                paper = {
                    "site_id": self.site_id,
                    "external_id": external_id,
                    "post_number": post_number,
                    "title": title,
                    "abstract": abstract,
                    "published_date": published_date,
                    "posted_date": None,
                    "authors": None,
                    "publisher": "ASTRON – Netherlands Institute for Radio Astronomy",
                    "department": None,
                    "journal": None,
                    "url": _LIST_URL,
                    "pdf_url": pdf_url,
                    "keywords": "radio astronomy, annual report, ASTRON, Netherlands",
                    "category": "Annual Report",
                    "doi": None,
                    "original_filename": original_filename,
                    "metadata": json.dumps(metadata, ensure_ascii=False),
                }

                self._save_paper(paper)
                saved += 1
                print(f"[{self.site_id}] saved {saved}/{limit_display}: {title}")

                time.sleep(self._delay)

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {idx} failed: {exc}")
                continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
