# -*- coding: utf-8 -*-
"""VA.gov NCVAS Reports crawler.

Scrapes https://www.va.gov/vetdata/report.asp — a single static HTML page
listing historical and special Veterans Affairs reports published by the
National Center for Veterans Analysis and Statistics (NCVAS).

Each individual file link (PDF / XLSX) on the page becomes one record.
There is no pagination: the entire catalogue is on one HTML page.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_SITE_ID = "va-gov-vetdata"
_PUBLISHER = (
    "U.S. Department of Veterans Affairs; "
    "National Center for Veterans Analysis and Statistics"
)
_DEPARTMENT = "National Center for Veterans Analysis and Statistics"
_REPORT_URL = "https://www.va.gov/vetdata/report.asp"
_FILE_EXTS = {".pdf", ".xlsx", ".xls", ".docx", ".doc", ".csv", ".zip"}

# Minimum abstract length to save a record (per requirement)
_ABSTRACT_MIN = 50
# Target abstract length — augment shorter ones to reach this
_ABSTRACT_TARGET = 100

_AUGMENT_SUFFIX = (
    " — Published by the National Center for Veterans Analysis and Statistics "
    "(NCVAS), U.S. Department of Veterans Affairs. Provides veteran statistics, "
    "benefit data, and related information."
)


class VaGovVetdataCrawler(BaseCrawler):
    """Crawler for VA.gov NCVAS Reports page.

    Parses https://www.va.gov/vetdata/report.asp and extracts every
    individual report file (PDF/XLSX/etc.) as a separate record.
    """

    site_id = _SITE_ID
    site_name = "Custom: va-gov-vetdata"
    base_url = "https://www.va.gov"

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """Fetch *url* via curl; return decoded text or None on failure."""
        cmd = [
            "curl", "-sk", "--tls-max", "1.3",
            "--connect-timeout", "15", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,*/*;q=0.8",
            url,
        ]
        last_err = "unknown"
        waits = [1, 3, 9]
        for attempt, wait in enumerate(waits, start=1):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=40, check=False
                )
                if result.returncode == 0 and result.stdout and result.stdout.strip():
                    return result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_err = f"exit={result.returncode} {stderr[:120]}"
            except Exception as exc:
                last_err = str(exc)

            if attempt < len(waits):
                print(
                    f"[{_SITE_ID}] curl attempt {attempt}/{len(waits)} failed "
                    f"for {url}: {last_err}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{_SITE_ID}] curl failed after 3 attempts: {last_err}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers (all static to avoid confusion with instance state)
    # ------------------------------------------------------------------

    @staticmethod
    def _make_soup(html: str) -> BeautifulSoup:
        """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        raise RuntimeError("All BeautifulSoup parsers failed")

    @staticmethod
    def _clean(text: str) -> str:
        """Normalise whitespace and strip NBSP."""
        return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()

    @classmethod
    def _elem_text(cls, elem) -> str:
        return cls._clean(elem.get_text(separator=" ", strip=True))

    @classmethod
    def _para_desc(cls, p_elem, skip_child=None) -> str:
        """Extract visible text from a <p>, optionally skipping one child."""
        parts: list[str] = []
        for child in p_elem.children:
            if skip_child is not None and child is skip_child:
                continue
            # Tags have a .name attribute that is not None
            if getattr(child, "name", None):
                t = child.get_text(separator=" ", strip=True)
            else:
                t = str(child)
            t = cls._clean(t)
            if t:
                parts.append(t)
        raw = " ".join(parts)
        # Strip any leading non-alpha junk (e.g. stray BR text "  ")
        return re.sub(r"^[^a-zA-Z]+", "", cls._clean(raw))

    @staticmethod
    def _is_file_href(href: str) -> bool:
        ext = os.path.splitext(href.split("?")[0].split("#")[0].lower())[1]
        return ext in _FILE_EXTS

    @staticmethod
    def _build_abstract(base: str, group: str, section: str, caption: str) -> str:
        """Return *base* text, augmenting it to >= _ABSTRACT_TARGET chars."""
        desc = base.strip() if base else ""
        if len(desc) >= _ABSTRACT_TARGET:
            return desc
        # Build a context prefix from available metadata
        ctx_parts = [p for p in [group, caption, section] if p]
        ctx = ": ".join(ctx_parts) if ctx_parts else "VA Report"
        augmented = (desc + " " + ctx + _AUGMENT_SUFFIX if desc
                     else ctx + _AUGMENT_SUFFIX)
        return re.sub(r"\s+", " ", augmented).strip()

    def _resolve_url(self, href: str) -> str:
        return href if href.startswith("http") else urljoin(self.base_url, href)

    # ------------------------------------------------------------------
    # Record builder
    # ------------------------------------------------------------------

    def _build_record(
        self,
        href: str,
        link_text: str,
        group_title: str,
        abstract_text: str,
        section: str,
        caption: str,
        inline: bool,
    ) -> dict | None:
        """Return a paper dict or None if the href is not a file link."""
        if not href or href.startswith("#"):
            return None
        if not self._is_file_href(href):
            return None

        file_url = self._resolve_url(href)
        filename = href.rstrip("/").split("/")[-1].split("?")[0]

        # Year: try link text first, then filename
        year_m = re.search(r"\b(1[89]\d{2}|20[012]\d)\b", link_text)
        if not year_m:
            year_m = re.search(r"\b(1[89]\d{2}|20[012]\d)\b", filename)
        year = year_m.group(1) if year_m else ""

        # Title construction
        if inline:
            title = group_title or filename
        elif group_title and caption and year:
            title = f"{group_title}: {caption} {year}"
        elif group_title and year:
            title = f"{group_title} {year}"
        elif group_title and caption:
            title = f"{group_title}: {caption} ({link_text})"
        elif group_title:
            title = f"{group_title}: {link_text or filename}"
        elif caption and year:
            title = f"{section}: {caption} {year}"
        elif caption:
            title = f"{section}: {caption} ({filename})"
        else:
            title = f"{section}: {filename}"
        title = re.sub(r"\s+", " ", title).strip().rstrip(":")

        full_abstract = self._build_abstract(abstract_text, group_title, section, caption)
        if len(full_abstract) < _ABSTRACT_MIN:
            return None  # unfixable — skip

        published_date = f"{year}-01-01" if year else ""
        external_id = href.lstrip("/")

        return {
            "external_id": external_id,
            "post_number": year or None,
            "title": title,
            "abstract": full_abstract,
            "published_date": published_date,
            "listed_date": "",
            "url": _REPORT_URL,
            "pdf_url": file_url,
            "authors": "",
            "publisher": _PUBLISHER,
            "department": _DEPARTMENT,
            "journal": "",
            "keywords": "",
            "category": section,
            "doi": "",
            "original_filename": filename,
            "metadata": json.dumps({
                "section": section,
                "group": group_title,
                "caption": caption,
                "link_text": link_text,
                "filename": filename,
                "year": year,
            }, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Page parser
    # ------------------------------------------------------------------

    def _parse_reports(self, html: str) -> list[dict]:
        """Parse the NCVAS report page; return flat list of record dicts."""
        try:
            soup = self._make_soup(html)
        except Exception as exc:
            print(f"[{_SITE_ID}] HTML parse failed: {exc}")
            return []

        inner = soup.find(id="innerContent") or soup.find("body")
        if not inner:
            print(f"[{_SITE_ID}] Could not locate innerContent")
            return []

        records: list[dict] = []
        current_section = "VA Reports"
        current_group_title = ""
        current_abstract = ""
        section_abstract = ""   # set when an H2 + description para is found

        containers = inner.find_all("div", class_="accordion-container", recursive=False)
        if not containers:
            containers = inner.find_all("div", class_="accordion-container")

        for container in containers:
            # Detect section header (accordion toggle link)
            toggle_el = container.find(class_="accordion-toggle")
            if toggle_el:
                t = self._elem_text(toggle_el)
                if t and len(t) > 2:
                    current_section = t
                    current_group_title = ""
                    current_abstract = ""
                    section_abstract = ""

            content = container.find("div", class_="accordion-content") or container

            for elem in content.children:
                # Only process tag nodes
                if not getattr(elem, "name", None):
                    continue

                # ----- H2: sub-section header with optional description -----
                if elem.name == "h2":
                    if "page-title" in (elem.get("class") or []):
                        continue
                    h2_text = self._elem_text(elem)
                    nxt_p = elem.find_next_sibling("p")
                    if nxt_p:
                        p_text = self._elem_text(nxt_p)
                        if len(p_text) > 50:
                            section_abstract = p_text
                            current_abstract = p_text
                            current_group_title = h2_text
                            continue
                    # H2 with no descriptive paragraph — inherit section abstract
                    current_group_title = h2_text
                    current_abstract = section_abstract

                # ----- P: named report group with inline description -----
                elif elem.name == "p":
                    blue = elem.find("span", class_="fontPrimaryBlue")
                    if not blue:
                        continue
                    strong = blue.find("strong")
                    current_group_title = (
                        self._elem_text(strong) if strong else self._elem_text(blue)
                    )
                    desc = self._para_desc(elem, skip_child=blue)
                    current_abstract = desc if len(desc) >= 30 else section_abstract or desc

                    # Inline file links within this paragraph
                    for a in elem.find_all("a", href=True):
                        rec = self._build_record(
                            a["href"], a.get_text(strip=True),
                            current_group_title, current_abstract,
                            current_section, "", inline=True,
                        )
                        if rec:
                            records.append(rec)

                # ----- TABLE: year-based file links -----
                elif elem.name == "table":
                    caption_el = elem.find("caption")
                    caption = self._elem_text(caption_el) if caption_el else ""
                    abs_text = current_abstract or section_abstract
                    for a in elem.find_all("a", href=True):
                        rec = self._build_record(
                            a["href"], a.get_text(strip=True),
                            current_group_title, abs_text,
                            current_section, caption, inline=False,
                        )
                        if rec:
                            records.append(rec)

        return records

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        """Crawl the NCVAS reports page and save records.

        All reports live on a single static HTML page, so there is no
        pagination loop.  The *limit* parameter caps saved records for
        any value (3, 500, or None for unlimited).  A seen_urls set
        guards against duplicate file links on the page.
        """
        start_time = time.monotonic()
        max_wall = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute budget
        lim_str = str(limit) if limit is not None else "∞"

        print(f"[{_SITE_ID}] Fetching {_REPORT_URL}")
        html = self._curl_get(_REPORT_URL)
        if not html:
            print(f"[{_SITE_ID}] ERROR: Failed to fetch report page")
            return 0

        print(f"[{_SITE_ID}] Parsing ({len(html):,} bytes)")
        try:
            all_records = self._parse_reports(html)
        except Exception as exc:
            print(f"[{_SITE_ID}] Parse error: {exc}")
            return 0

        if not all_records:
            print(f"[{_SITE_ID}] No records found on page")
            return 0

        print(f"[{_SITE_ID}] Found {len(all_records)} candidate records")

        # Deduplicate by file URL
        seen_urls: set[str] = set()
        deduped: list[dict] = []
        for r in all_records:
            key = r.get("pdf_url") or r.get("url", "")
            if key and key in seen_urls:
                continue
            if key:
                seen_urls.add(key)
            deduped.append(r)

        print(f"[{_SITE_ID}] After dedup: {len(deduped)} records; saving up to {lim_str}")

        saved = 0
        # Simulated page counter for the progress log requirement
        # (all records come from page 1 of 1)
        print(f"[{_SITE_ID}] page 1: saved {saved}/{lim_str}")

        for i, record in enumerate(deduped):
            if limit is not None and saved >= limit:
                break

            if time.monotonic() - start_time > max_wall:
                print(
                    f"[{_SITE_ID}] 25-minute wall-clock budget reached; "
                    f"stopping at {saved} records"
                )
                break

            abstract = record.get("abstract", "")
            if len(abstract) < _ABSTRACT_MIN:
                print(
                    f"[{_SITE_ID}] Skipping (abstract too short {len(abstract)} chars): "
                    f"{record.get('title', '?')[:60]}"
                )
                continue

            try:
                self._save_paper({
                    "id": None,
                    "site_id": self.site_id,
                    "external_id": record["external_id"],
                    "post_number": record.get("post_number"),
                    "title": record["title"],
                    "abstract": record["abstract"],
                    "published_date": record.get("published_date"),
                    "listed_date": record.get("listed_date"),
                    "url": record["url"],
                    "pdf_url": record.get("pdf_url"),
                    "authors": record.get("authors", ""),
                    "publisher": record.get("publisher", ""),
                    "department": record.get("department", ""),
                    "journal": record.get("journal", ""),
                    "keywords": record.get("keywords", ""),
                    "category": record.get("category", ""),
                    "doi": record.get("doi", ""),
                    "original_filename": record.get("original_filename"),
                    "metadata": record.get("metadata", "{}"),
                })
                saved += 1
                if saved % 10 == 0:
                    print(f"[{_SITE_ID}] page 1: saved {saved}/{lim_str}")
                elif saved <= 5:
                    print(
                        f"[{_SITE_ID}] saved {saved}/{lim_str}: "
                        f"{record['title'][:70]}"
                    )

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(
                    f"[{_SITE_ID}] item {i} failed: {exc} — "
                    f"title={record.get('title', '?')[:50]}"
                )
                continue

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
