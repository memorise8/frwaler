# -*- coding: utf-8 -*-
"""Crawler for Forskningsradet (Norwegian Research Council) publications."""

from __future__ import annotations

import io
import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class ForskningsradetNomOmForskningsradetCrawler(BaseCrawler):
    site_id = "forskningsradet-no-om-forskningsradet"
    site_name = "Custom: forskningsradet-no-om-forskningsradet"
    base_url = "https://www.forskningsradet.no"

    _LIST_URL = "https://www.forskningsradet.no/om-forskningsradet/publikasjoner/"
    _MIN_ABSTRACT_CHARS = 100
    _MAX_PAGES = 200
    _MAX_WALL_SECS = 25 * 60

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url, *, timeout=60, binary=False):
        """Fetch URL via curl with up to 3 retries and exponential backoff."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--connect-timeout", "15", "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: */*",
            "-H", "Accept-Language: no,en;q=0.9",
            url,
        ]
        waits = [1, 3, 9]
        last_error = "unknown"
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 15, check=False
                )
                if result.returncode == 0 and result.stdout and result.stdout.strip():
                    if binary:
                        return result.stdout
                    return result.stdout.decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} stderr={stderr[:120]}"
            except Exception as exc:
                last_error = str(exc)

            if attempt < 2:
                w = waits[attempt]
                print(
                    f"[{self.site_id}] curl failed {attempt+1}/3 for {url}: "
                    f"{last_error}; retrying in {w}s"
                )
                time.sleep(w)

        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    # ------------------------------------------------------------------
    # HTML / props parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_html(raw):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                print(f"[forskningsradet-no-om-forskningsradet] BeautifulSoup({parser}) failed: {exc}")
        return None

    def _extract_props(self, raw):
        """Pull the PublicationsPage React props JSON from the HTML."""
        if not raw:
            return None
        idx = raw.find("PublicationsPage")
        if idx < 0:
            return None
        props_label = raw.find("props:", idx)
        if props_label < 0:
            return None
        start = props_label + 6
        while start < len(raw) and raw[start] in " \t\n\r":
            start += 1
        if start >= len(raw) or raw[start] != "{":
            return None

        depth = 0
        in_str = False
        esc = False
        for i, c in enumerate(raw[start:]):
            if esc:
                esc = False
            elif c == "\\" and in_str:
                esc = True
            elif c == '"' and not esc:
                in_str = not in_str
            elif not in_str:
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(raw[start : start + i + 1])
                        except Exception:
                            return None
        return None

    def _find_search_results(self, obj, acc=None):
        """Recursively collect SearchResult componentData dicts."""
        if acc is None:
            acc = []
        if isinstance(obj, dict):
            if obj.get("componentName") == "SearchResult":
                data = obj.get("componentData")
                if isinstance(data, dict):
                    acc.append(data)
            for v in obj.values():
                self._find_search_results(v, acc)
        elif isinstance(obj, list):
            for item in obj:
                self._find_search_results(item, acc)
        return acc

    def _get_years(self, props):
        """Return all year values from sections.list (enabled + disabled)."""
        years = []
        for s in props.get("sections", {}).get("list", []):
            if s.get("name") == "year":
                val = s.get("value", "")
                if val:
                    years.append(val)
        return years

    # ------------------------------------------------------------------
    # Metadata helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_meta_items(items):
        result = {}
        for item in items or []:
            label = (item.get("label") or "").strip()
            text = (item.get("text") or "").strip()
            if label and text:
                result[label] = text
        return result

    @staticmethod
    def _clean_text(text):
        return re.sub(r"\s+", " ", (text or "")).strip()

    # ------------------------------------------------------------------
    # PDF text extraction
    # ------------------------------------------------------------------

    def _extract_pdf_abstract(self, pdf_url):
        """Download PDF and return extracted text from first 3 pages.

        Returns empty string on any failure.
        """
        full_url = urljoin(self.base_url, pdf_url)
        pdf_bytes = self._curl(full_url, timeout=90, binary=True)
        if not pdf_bytes or len(pdf_bytes) < 200:
            return ""

        try:
            from pdfminer.high_level import extract_text as _pdf_extract
            text = _pdf_extract(io.BytesIO(pdf_bytes), maxpages=3)
            return self._clean_text(text or "")
        except Exception as exc:
            print(f"[{self.site_id}] PDF text extraction failed for {pdf_url}: {exc}")

        # Fallback: try pypdf
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
            parts = []
            for page in reader.pages[:3]:
                parts.append(page.extract_text() or "")
            return self._clean_text(" ".join(parts))
        except Exception as exc2:
            print(f"[{self.site_id}] pypdf fallback failed for {pdf_url}: {exc2}")
            return ""

    # ------------------------------------------------------------------
    # Year-page fetcher
    # ------------------------------------------------------------------

    def _fetch_year_items(self, year):
        """Return list of SearchResult componentData dicts for one year."""
        url = f"{self._LIST_URL}?year={year}"
        raw = self._curl(url, timeout=60)
        if not raw:
            return []
        props = self._extract_props(raw)
        if not props:
            return []
        return self._find_search_results(props)

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        saved = 0
        seen_urls: set[str] = set()

        # 1. Fetch main page to discover available years
        raw_main = self._curl(self._LIST_URL, timeout=60)
        if not raw_main:
            print(f"[{self.site_id}] Failed to fetch main page; aborting")
            return 0

        props_main = self._extract_props(raw_main)
        if not props_main:
            print(f"[{self.site_id}] Failed to parse main page props; aborting")
            return 0

        years = self._get_years(props_main)
        if not years:
            # Fallback: generate years 2026..2000
            years = [str(y) for y in range(2026, 1999, -1)]

        print(f"[{self.site_id}] Discovered {len(years)} year sections: {years[:6]}...")

        page_num = 0

        for year in years:
            if limit is not None and saved >= limit:
                break
            if page_num >= self._MAX_PAGES:
                print(
                    f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached; stopping"
                )
                break

            elapsed = time.time() - start_time
            if elapsed >= self._MAX_WALL_SECS:
                print(
                    f"[{self.site_id}] Wall-clock budget reached ({elapsed:.0f}s); stopping"
                )
                break

            page_num += 1
            if page_num % 10 == 0:
                limit_str = str(limit) if limit is not None else "inf"
                print(
                    f"[{self.site_id}] page {page_num}: saved {saved}/{limit_str}"
                )

            items = self._fetch_year_items(year)
            if not items:
                print(f"[{self.site_id}] year {year}: 0 items, skipping")
                continue

            print(f"[{self.site_id}] year {year}: {len(items)} items found")

            for idx, item in enumerate(items):
                if limit is not None and saved >= limit:
                    break

                elapsed = time.time() - start_time
                if elapsed >= self._MAX_WALL_SECS:
                    print(
                        f"[{self.site_id}] Wall-clock budget reached ({elapsed:.0f}s); stopping"
                    )
                    break

                try:
                    pdf_path = (item.get("url") or "").strip()
                    if not pdf_path:
                        continue

                    full_pdf_url = urljoin(self.base_url, pdf_path)

                    # URL deduplication
                    if full_pdf_url in seen_urls:
                        continue
                    seen_urls.add(full_pdf_url)

                    title = self._clean_text(unescape(item.get("title") or ""))
                    if not title:
                        print(f"[{self.site_id}] item {idx+1} (year {year}): missing title, skip")
                        continue

                    # Parse metadata fields
                    meta_raw = self._parse_meta_items(
                        item.get("metadata", {}).get("items", [])
                    )
                    pub_type = (
                        meta_raw.get("Publikasjonstype")
                        or meta_raw.get("Publication type", "")
                    )
                    pub_year_raw = (
                        meta_raw.get("Publiseringsår")
                        or meta_raw.get("Year of publication", year)
                    )
                    pages = meta_raw.get("Antall sider") or meta_raw.get("Number of pages", "")
                    isbn = meta_raw.get("ISBN", "")
                    doc_info = meta_raw.get("Dokument") or meta_raw.get("Document", "")
                    subtitle = self._clean_text(item.get("text") or "")

                    # Normalise published year → YYYY
                    pub_year_str = str(pub_year_raw).strip()
                    m = re.search(r"\b(19|20)\d{2}\b", pub_year_str)
                    published_date = m.group(0) if m else pub_year_str
                    listed_date = published_date

                    # post_number / external_id from PDF filename (without extension)
                    filename = pdf_path.rstrip("/").split("/")[-1]
                    original_filename = filename
                    post_number = re.sub(r"\.pdf$", "", filename, flags=re.I)
                    external_id = post_number

                    # Rate-limit before PDF download
                    time.sleep(self._delay)

                    abstract = self._extract_pdf_abstract(pdf_path)

                    if len(abstract) < self._MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {idx+1} (year {year}) skipped: "
                            f"abstract too short ({len(abstract)} chars): {title[:60]}"
                        )
                        continue

                    # Truncate to a reasonable length
                    abstract = abstract[:4000]

                    metadata_dict = {
                        "posted_date": pub_year_str,
                        "originalFilename": original_filename,
                        "pub_type": pub_type,
                        "pages": pages,
                        "isbn": isbn,
                        "doc_info": doc_info,
                        "subtitle": subtitle,
                        "year_section": year,
                        "raw_metadata": meta_raw,
                    }
                    metadata_dict = {k: v for k, v in metadata_dict.items() if v}

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "authors": "",
                        "publisher": "Norges forskningsråd",
                        "department": "",
                        "journal": "",
                        "url": full_pdf_url,
                        "pdf_url": full_pdf_url,
                        "keywords": pub_type,
                        "category": pub_type,
                        "doi": "",
                        "original_filename": original_filename,
                        "metadata": json.dumps(metadata_dict, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    limit_str = str(limit) if limit is not None else "inf"
                    print(
                        f"[{self.site_id}] saved {saved}/{limit_str}: {title[:70]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {idx+1} (year {year}) failed: {exc}")
                    continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
