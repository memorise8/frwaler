# -*- coding: utf-8 -*-
"""Crawler for U.S. Department of the Treasury – GAO-IG Act Reports.

Starting URL:
  https://home.treasury.gov/about/budget-financial-reporting-planning-and-performance/
  good-accounting-obligation-in-government-act-gao-ig-act-reports

Single static Drupal 10 page listing PDF GAO-IG Act report documents.
No API, no pagination — one HTML scrape, then per-PDF text extraction.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from html import unescape
from urllib.parse import urljoin, urlparse

sys.path.insert(0, '.')
from crawler.base_crawler import BaseCrawler

_SITE_ID = "home-treasury-gov-about"
_BASE_URL = "https://home.treasury.gov"
_START_URL = (
    "https://home.treasury.gov/about/budget-financial-reporting-planning-and-performance"
    "/good-accounting-obligation-in-government-act-gao-ig-act-reports"
)

_FALLBACK_INTRO = (
    "The Good Accounting Obligation in Government Act (GAO-IG Act) requires each agency "
    "to include, in its annual budget justification, a report that identifies each public "
    "recommendation issued by the Government Accountability Office (GAO) and the agency's "
    "inspectors general (IGs) which has remained unimplemented for one year or more from "
    "the annual budget justification submission date. In addition, the Act requires a "
    "reconciliation between the agency records and the IGs' Semiannual Report to Congress "
    "(SAR). In compliance with the GAO-IG Act, Treasury provides reports listing each public "
    "recommendation from GAO, Treasury's Office of the Inspector General (OIG), and Treasury "
    "Inspector General for Tax Administration (TIGTA)."
)

_PUBLISHER = "U.S. Department of the Treasury"
_DEPARTMENT = "Budget, Financial Reporting, Planning and Performance"


class HomeTreasuryGovAboutCrawler(BaseCrawler):

    site_id = _SITE_ID
    site_name = "Custom: home-treasury-gov-about"
    base_url = _BASE_URL

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl(self, url: str, *, timeout: int = 45) -> str | None:
        """Fetch URL via curl with 3-attempt exponential backoff. Returns text or None."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            url,
        ]
        waits = [1, 3, 9]
        last_err = ""
        for attempt, wait in enumerate(waits, start=1):
            try:
                res = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 10, check=False
                )
                raw = res.stdout.decode("utf-8", errors="replace")
                if res.returncode == 0 and raw.strip():
                    return raw
                last_err = f"exit={res.returncode}"
            except Exception as exc:
                last_err = str(exc)
            if attempt < len(waits):
                print(
                    f"[{_SITE_ID}] curl attempt {attempt} failed ({url}): "
                    f"{last_err}; retry in {wait}s"
                )
                time.sleep(wait)
        print(f"[{_SITE_ID}] curl failed after 3 attempts for {url}: {last_err}")
        return None

    def _curl_binary(self, url: str, dest: str, *, timeout: int = 60) -> bool:
        """Download binary URL to dest path. Returns True on success."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-o", dest,
            url,
        ]
        waits = [1, 3, 9]
        last_err = ""
        for attempt, wait in enumerate(waits, start=1):
            try:
                res = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 10, check=False
                )
                if res.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 0:
                    return True
                last_err = f"exit={res.returncode}"
            except Exception as exc:
                last_err = str(exc)
            if attempt < len(waits):
                print(
                    f"[{_SITE_ID}] download attempt {attempt} failed ({url}): "
                    f"{last_err}; retry in {wait}s"
                )
                time.sleep(wait)
        print(f"[{_SITE_ID}] download failed after 3 attempts: {url}: {last_err}")
        return False

    # ------------------------------------------------------------------
    # PDF text extraction
    # ------------------------------------------------------------------

    def _extract_pdf_abstract(self, pdf_url: str, max_chars: int = 2000) -> str:
        """Download PDF and extract first 2 pages text via pdftotext.

        Returns cleaned text up to max_chars, or empty string on failure.
        """
        tmp_path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp_path = tmp.name

            if not self._curl_binary(pdf_url, tmp_path):
                return ""

            res = subprocess.run(
                ["pdftotext", "-f", "1", "-l", "2", tmp_path, "-"],
                capture_output=True,
                timeout=30,
            )
            if res.returncode != 0:
                return ""

            text = res.stdout.decode("utf-8", errors="replace")
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\n{3,}", "\n\n", text)
            text = text.strip()
            return text[:max_chars]
        except Exception as exc:
            print(f"[{_SITE_ID}] PDF extraction failed for {pdf_url}: {exc}")
            return ""
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_soup(html: str):
        """BeautifulSoup with html5lib → lxml → html.parser fallback. Returns soup or None."""
        from bs4 import BeautifulSoup
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _extract_fy_year(text: str) -> str | None:
        """Return fiscal year from text, e.g. 'FY 2025' → '2025'."""
        m = re.search(r"FY\s*(\d{4})", text, re.IGNORECASE)
        if m:
            return m.group(1)
        m = re.search(r"\b(20\d\d)\b", text)
        return m.group(1) if m else None

    # ------------------------------------------------------------------
    # Page parsing
    # ------------------------------------------------------------------

    def _parse_listing(self, html: str) -> tuple[str, str, list[dict]]:
        """Parse the GAO-IG Act reports listing page.

        Returns (page_intro_text, page_date, list_of_doc_dicts).
        Each doc_dict: title, href, full_url, filename, category.
        """
        soup = self._make_soup(html)
        if soup is None:
            return _FALLBACK_INTRO, "", []

        # Page date from meta tags
        page_date = ""
        for prop in ("og:updated_time", "article:modified_time", "article:published_time"):
            tag = soup.find("meta", property=prop)
            if tag and tag.get("content"):
                page_date = tag["content"][:10]
                break

        # Page intro: body div text before the first link, or fallback
        page_intro = _FALLBACK_INTRO
        body_div = (
            soup.find("div", class_="field--name-field-page-body")
            or soup.find("article")
            or soup.find("div", id="content")
        )
        if body_div:
            intro_parts = []
            for elem in body_div.children:
                try:
                    if hasattr(elem, "find") and elem.find("a", href=True):
                        break
                    t = (
                        elem.get_text(separator=" ", strip=True)
                        if hasattr(elem, "get_text")
                        else str(elem).strip()
                    )
                    if t:
                        intro_parts.append(t)
                except Exception:
                    pass
            intro_text = re.sub(r"\s+", " ", " ".join(intro_parts)).strip()
            if len(intro_text) >= 100:
                page_intro = intro_text

        # Collect all PDF links from the content area
        search_root = body_div if body_div else soup
        docs: list[dict] = []
        seen_hrefs: set[str] = set()

        for a in search_root.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            if not href or ".pdf" not in href.lower():
                continue
            if href in seen_hrefs:
                continue
            seen_hrefs.add(href)

            title = re.sub(
                r"\s+", " ", unescape(a.get_text(separator=" ", strip=True))
            ).strip()
            if len(title) < 3:
                continue

            full_url = href if href.startswith("http") else urljoin(_BASE_URL, href)
            filename = os.path.basename(urlparse(href).path.split("?")[0])

            category = (
                "GAO-IG Act Report Executive Summary"
                if "executive summary" in title.lower()
                else "GAO-IG Act Report"
            )

            docs.append({
                "title": title,
                "href": href,
                "full_url": full_url,
                "filename": filename,
                "category": category,
            })

        return page_intro, page_date, docs

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_ts = time.time()
        saved = 0
        seen_urls: set[str] = set()
        limit_or_inf = limit if limit is not None else float("inf")

        print(f"[{_SITE_ID}] Starting crawl (limit={limit})")

        # Fetch the single listing page (no further pagination for this site).
        raw = self._curl(_START_URL)
        if not raw:
            print(f"[{_SITE_ID}] Failed to fetch listing page. Aborting.")
            return 0

        page_intro, page_date, doc_list = self._parse_listing(raw)
        if not doc_list:
            print(f"[{_SITE_ID}] No document links found. Aborting.")
            return 0

        total_found = len(doc_list)
        print(
            f"[{_SITE_ID}] Found {total_found} candidate document links; "
            f"limit={limit if limit is not None else '∞'}"
        )

        # Single listing page — log progress at start (page 1)
        page = 1
        print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit if limit is not None else '∞'}")

        for idx, doc in enumerate(doc_list):
            if saved >= limit_or_inf:
                break

            # 25-minute wall-clock budget
            if time.time() - start_ts > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached; stopping.")
                break

            # Progress log every 10 items
            if idx > 0 and idx % 10 == 0:
                print(
                    f"[{_SITE_ID}] page {page}: saved {saved}/{limit if limit is not None else '∞'}"
                )

            full_url = doc["full_url"]

            # URL-based deduplication across pages
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)

            try:
                title = doc["title"]
                category = doc["category"]
                filename = doc["filename"]
                href = doc["href"]

                # external_id: PDF filename without extension (unique per document)
                external_id = re.sub(r"\.pdf$", "", filename, flags=re.IGNORECASE)[:200]

                # post_number: fiscal year string for incremental MAX() tracking
                fy_year = self._extract_fy_year(title)
                post_number = fy_year  # e.g. "2025"

                # published_date: from page meta or inferred from FY year
                # FY N budget is submitted to Congress ~March of year N-1
                published_date = page_date or None
                if not published_date and fy_year:
                    pub_yr = int(fy_year) - 1
                    published_date = f"{pub_yr}-03-01"

                # Abstract: extract first 2 pages of PDF text
                print(f"[{_SITE_ID}] Extracting PDF text: {title[:60]}")
                abstract = self._extract_pdf_abstract(full_url, max_chars=2000)

                if not abstract or len(abstract) < 50:
                    # Fallback: page intro description + document-specific context
                    ctx = f" This document is the {title}"
                    if fy_year:
                        ctx += f" for fiscal year {fy_year}"
                    ctx += "."
                    abstract = re.sub(r"\s+", " ", (page_intro + ctx)).strip()

                if len(abstract) < 50:
                    print(f"[{_SITE_ID}] Skipping '{title[:50]}': abstract < 50 chars")
                    continue

                kw_parts = ["GAO", "IG Act", "Treasury", "accountability", "recommendations"]
                if fy_year:
                    kw_parts.append(f"FY{fy_year}")
                if "executive summary" in title.lower():
                    kw_parts.append("executive summary")
                keywords = ",".join(kw_parts)

                paper = {
                    "site_id": _SITE_ID,
                    "external_id": external_id,
                    "post_number": post_number,
                    "title": title,
                    "abstract": abstract,
                    "published_date": published_date,
                    "listed_date": published_date,
                    "url": _START_URL,
                    "pdf_url": full_url,
                    "publisher": _PUBLISHER,
                    "department": _DEPARTMENT,
                    "authors": None,
                    "journal": None,
                    "category": category,
                    "keywords": keywords,
                    "doi": None,
                    "original_filename": filename,
                    "metadata": json.dumps(
                        {
                            "posted_date": published_date,
                            "originalFilename": filename,
                            "fiscal_year": fy_year,
                            "report_type": category,
                            "href": href,
                        },
                        ensure_ascii=False,
                    ),
                }

                self._save_paper(paper)
                saved += 1
                print(
                    f"[{_SITE_ID}] Saved {saved}/{limit if limit is not None else '∞'}: "
                    f"{title[:70]}"
                )

                time.sleep(self._delay)

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] item {idx + 1} failed: {exc}")
                continue

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
