# -*- coding: utf-8 -*-
"""Crawler for DIA NZ Annual Reports.

Starting URL:
    https://www.dia.govt.nz/diawebsite.nsf/wpg_URL/
    Resource-material-Corporate-Publications-Annual-Reports?OpenDocument

The page is a single HTML listing of annual reports dating back to 1996/97.
Each entry has a title (h2/h3), a PDF link, and—for older reports—a separate
detail page with DC metadata.  Recent reports link to an HTML version page
that carries DC meta and rich body text.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_SITE_ID = "dia-govt-nz-diawebsitensf"
_BASE_URL = "https://www.dia.govt.nz"
_LIST_URL = (
    "https://www.dia.govt.nz/diawebsite.nsf/wpg_URL/"
    "Resource-material-Corporate-Publications-Annual-Reports?OpenDocument"
)
_ABSTRACT_MIN_CHARS = 50
_MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))


class DIAGovtNZCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: dia-govt-nz-diawebsitensf"
    base_url = _BASE_URL

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl(self, url: str, *, timeout: int = 45) -> str | None:
        """GET via curl with 3-attempt exponential backoff (1s, 3s, 9s)."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: en-NZ,en;q=0.9",
            url,
        ]
        waits = [1, 3, 9]
        for attempt, wait in enumerate(waits, start=1):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
                if r.returncode == 0 and r.stdout:
                    return r.stdout.decode("utf-8", errors="replace")
                print(f"[{_SITE_ID}] curl empty/non-zero (attempt {attempt}) for {url}")
            except Exception as exc:
                print(f"[{_SITE_ID}] curl error (attempt {attempt}): {exc}")
            if attempt < len(waits):
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # BeautifulSoup with fallback parser chain
    # ------------------------------------------------------------------

    @staticmethod
    def _parse(html: str) -> BeautifulSoup | None:
        """Parse with html5lib → lxml → html.parser fallback chain."""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------
    # Meta tag helper
    # ------------------------------------------------------------------

    @staticmethod
    def _meta(soup: BeautifulSoup, name: str) -> str:
        tag = soup.find("meta", attrs={"name": name})
        if tag:
            return (tag.get("content") or "").strip()
        return ""

    # ------------------------------------------------------------------
    # Year / date helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_year_range(title: str) -> tuple[int | None, int | None, str | None]:
        """Extract year range from heading text.

        Returns (start_year, end_year, slug) where slug is e.g. '2024-25'.
        """
        m = re.search(r'(\d{4})[/-](\d{2,4})', title)
        if m:
            start = int(m.group(1))
            end_raw = m.group(2)
            end = int(str(start)[:2] + end_raw) if len(end_raw) == 2 else int(end_raw)
            slug = f"{start}-{str(end)[2:]}"
            return start, end, slug
        m = re.search(r'(\d{4})', title)
        if m:
            yr = int(m.group(1))
            return yr, yr, str(yr)
        return None, None, None

    @staticmethod
    def _year_to_date(end_year: int | None) -> str | None:
        """Fiscal year end date (NZ government fiscal year ends 30 June)."""
        return f"{end_year}-06-30" if end_year else None

    # ------------------------------------------------------------------
    # Entry extraction from main listing page
    # ------------------------------------------------------------------

    def _extract_entries(self, soup: BeautifulSoup) -> list[dict]:
        """Return one dict per annual report year, newest first."""
        entries: list[dict] = []
        seen_slugs: set[str] = set()

        # Collect all h2/h3 headings that represent annual report years
        qualifying: list = []
        for h in soup.find_all(["h2", "h3"]):
            txt = h.get_text(separator=" ", strip=True)
            tl = txt.lower()
            if "annual report" not in tl:
                continue
            if any(x in tl for x in [
                "read or download", "read the html", "archives new zealand",
                "national library", "by section",
            ]):
                continue
            qualifying.append(h)

        for i, heading in enumerate(qualifying):
            title_text = heading.get_text(separator=" ", strip=True)
            start_y, end_y, slug = self._parse_year_range(title_text)

            if not slug or slug in seen_slugs:
                continue

            next_h = qualifying[i + 1] if i + 1 < len(qualifying) else None
            pdf_url: str | None = None
            detail_url: str | None = None
            html_url: str | None = None

            for el in heading.find_all_next(["a", "h2", "h3"]):
                if next_h is not None and el is next_h:
                    break
                if el.name != "a":
                    continue
                href = (el.get("href") or "").strip()
                if not href or href.startswith("#") or href.startswith("mailto:"):
                    continue

                if href.startswith("http"):
                    full = href
                elif href.startswith("/"):
                    full = _BASE_URL + href
                else:
                    full = _BASE_URL + "/" + href.lstrip("/")

                href_l = href.lower()

                if href_l.endswith(".pdf") or "/$file/" in href_l:
                    if pdf_url is None:
                        pdf_url = full
                elif "opendocument" in href_l:
                    if detail_url is None:
                        detail_url = full
                elif re.search(r"resource-material.*annual.report", href_l):
                    if detail_url is None:
                        detail_url = full
                elif re.search(r"/annual.report.?\d{4}", href_l) and not href_l.endswith(".pdf"):
                    if html_url is None:
                        html_url = full

            if not (pdf_url or detail_url or html_url):
                continue

            entries.append({
                "slug": slug,
                "title": title_text,
                "start_year": start_y,
                "end_year": end_y,
                "post_number": str(end_y) if end_y else None,
                "url": detail_url or html_url or pdf_url,
                "pdf_url": pdf_url,
                "detail_url": detail_url,
                "html_url": html_url,
                "published_date": self._year_to_date(end_y),
            })
            seen_slugs.add(slug)

        entries.sort(key=lambda e: e.get("end_year") or 0, reverse=True)
        return entries

    # ------------------------------------------------------------------
    # Per-entry enrichment
    # ------------------------------------------------------------------

    def _enrich(self, entry: dict) -> dict | None:
        """Fetch detail/HTML page to build a full abstract.

        Returns a paper dict ready for _save_paper, or None if the abstract
        remains too short after all attempts.
        """
        slug = entry["slug"]
        title = entry["title"]
        pdf_url = entry.get("pdf_url")
        detail_url = entry.get("detail_url")
        html_url = entry.get("html_url")
        canonical = entry.get("url")

        dc_description = ""
        dc_subject = ""
        dc_creator = "The Department of Internal Affairs"
        dc_publisher = "Corporate Information"
        dc_created = ""
        body_text = ""

        # Prefer detail page, then HTML version page
        for fetch_url in filter(None, [detail_url, html_url]):
            raw = self._curl(fetch_url)
            if not raw:
                continue
            try:
                s = self._parse(raw)
                if not s:
                    continue
                dc_description = (
                    self._meta(s, "DC.Description")
                    or self._meta(s, "description")
                )
                dc_subject = self._meta(s, "DC.Subject") or self._meta(s, "keywords")
                dc_creator = self._meta(s, "DC.Creator") or dc_creator
                dc_publisher = self._meta(s, "DC.Publisher") or dc_publisher
                created_raw = self._meta(s, "DC.Date.Created")
                if created_raw:
                    dc_created = re.sub(r"[^\d]", "-", created_raw).strip("-")

                # Extract meaningful body text from the content area
                content_div = s.find("div", class_="content")
                src = content_div or s.body or s
                raw_body = src.get_text(separator=" ", strip=True) if src else ""
                body_text = re.sub(r"\s+", " ", raw_body).strip()
            except Exception as exc:
                print(f"[{_SITE_ID}] Error parsing {fetch_url}: {exc}")
            break  # stop after first successful fetch

        # Build abstract from best available sources
        parts: list[str] = []
        if dc_description:
            parts.append(dc_description)
        if dc_subject and dc_subject not in parts:
            parts.append(dc_subject)
        if body_text:
            clean = body_text[:600]
            if clean not in parts:
                parts.append(clean)
        abstract = "\n\n".join(parts).strip()

        # Fallback: construct a descriptive abstract from known metadata
        if len(abstract) < _ABSTRACT_MIN_CHARS:
            yr_str = slug.replace("-", "/")
            abstract = (
                f"Department of Internal Affairs Annual Report {yr_str}. "
                f"This annual report covers the activities, performance, and financial "
                f"statements of the Department of Internal Affairs "
                f"(Te Tari Taiwhenua) New Zealand for the {yr_str} fiscal year. "
                f"Tabled in the New Zealand Parliament and published by the "
                f"Department of Internal Affairs, Wellington."
            )

        if len(abstract) < _ABSTRACT_MIN_CHARS:
            print(
                f"[{_SITE_ID}] Skipping '{title}': "
                f"abstract too short ({len(abstract)} chars)"
            )
            return None

        # Original filename from PDF URL path segment
        original_filename: str | None = None
        if pdf_url:
            path_seg = urlparse(pdf_url).path.split("/")[-1]
            if path_seg:
                original_filename = path_seg

        # Normalise published_date: prefer DC.Date.Created, then year-derived
        published_date = entry.get("published_date")
        if dc_created:
            date_parts = [p for p in dc_created.split("-") if p]
            if len(date_parts) >= 3:
                try:
                    published_date = (
                        f"{int(date_parts[0]):04d}-"
                        f"{int(date_parts[1]):02d}-"
                        f"{int(date_parts[2]):02d}"
                    )
                except ValueError:
                    pass

        return {
            "id": None,
            "site_id": _SITE_ID,
            "external_id": slug,
            "post_number": entry.get("post_number"),
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": None,
            "authors": None,
            "publisher": (
                f"{dc_creator}; {dc_publisher}"
                if dc_publisher and dc_publisher != dc_creator
                else dc_creator
            ),
            "department": "Corporate Information",
            "journal": None,
            "url": canonical,
            "pdf_url": pdf_url,
            "keywords": dc_subject or None,
            "category": "Annual Report",
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(
                {
                    "year_slug": slug,
                    "start_year": entry.get("start_year"),
                    "end_year": entry.get("end_year"),
                    "detail_url": detail_url,
                    "html_version_url": html_url,
                    "dc_description": dc_description,
                    "dc_subject": dc_subject,
                    "dc_creator": dc_creator,
                    "posted_date": dc_created or None,
                    "originalFilename": original_filename,
                },
                ensure_ascii=False,
            ),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit: int | None = None) -> int:
        """Crawl DIA NZ annual reports.

        Fetches the single listing page, extracts one entry per annual report
        year, enriches each via its detail/HTML-version page, and saves to DB.

        Parameters
        ----------
        limit:
            Maximum number of records to save.  None = unlimited.
        """
        start_time = time.time()
        saved = 0
        seen_urls: set[str] = set()
        MAX_PAGES = 200  # safety cap (not expected for this single-page site)
        page = 1

        print(f"[{_SITE_ID}] Fetching listing page...")
        raw = self._curl(_LIST_URL)
        if not raw:
            print(f"[{_SITE_ID}] Failed to fetch listing page. Aborting.")
            return 0

        soup = self._parse(raw)
        if not soup:
            print(f"[{_SITE_ID}] Failed to parse listing page. Aborting.")
            return 0

        entries = self._extract_entries(soup)
        total = len(entries)
        print(f"[{_SITE_ID}] Found {total} annual report entries.")

        if not entries:
            print(f"[{_SITE_ID}] No entries found on page {page}. Done.")
            return 0

        for idx, entry in enumerate(entries):
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time > _MAX_WALL_SECONDS:
                print(
                    f"[{_SITE_ID}] Wall-clock budget exceeded after "
                    f"{_MAX_WALL_SECONDS}s, stopping."
                )
                break
            if page >= MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {MAX_PAGES} pages reached, stopping.")
                break

            url = entry.get("url") or entry.get("pdf_url") or ""
            if url in seen_urls:
                continue
            seen_urls.add(url)

            if idx > 0 and idx % 10 == 0:
                lim_str = str(limit) if limit is not None else "∞"
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{lim_str}")

            try:
                time.sleep(self._delay)
                paper = self._enrich(entry)
                if paper is None:
                    continue
                self._save_paper(paper)
                saved += 1
                lim_str = str(limit) if limit is not None else "∞"
                print(f"[{_SITE_ID}] Saved {saved}/{lim_str}: {paper['title'][:60]}")
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] item {idx} ({entry.get('slug')}) failed: {exc}")
                continue

        # This site has a single listing page; after processing all entries once,
        # pagination naturally ends.
        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
