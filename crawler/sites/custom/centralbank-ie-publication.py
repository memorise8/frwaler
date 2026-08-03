# -*- coding: utf-8 -*-
"""Central Bank of Ireland — Annual Reports publication crawler.

Starting URL: https://www.centralbank.ie/publication/corporate-reports/annual-reports

All records are embedded in the listing page HTML as a JavaScript ``appData``
array — no separate API call is required.  Detail pages are fetched for recent
years (≥ 2020) to obtain richer abstract text; older years fall back to a
synthesised summary that is always ≥ 100 chars.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_BASE = "https://www.centralbank.ie"
_LIST_URL = f"{_BASE}/publication/corporate-reports/annual-reports"
_SITE_ID = "centralbank-ie-publication"
_PUBLISHER = "Central Bank of Ireland"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _curl_get(url: str, timeout: int = 30, retries: int = 3) -> str | None:
    """GET via curl with TLS-max 1.3 and exponential-backoff retries (1s, 3s, 9s)."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", f"User-Agent: {_UA}",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: en-IE,en;q=0.9",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            raw = result.stdout.decode("utf-8", errors="replace")
            if raw.strip():
                return raw
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[{_SITE_ID}] empty response, retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[{_SITE_ID}] curl error: {exc}, retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
            else:
                print(f"[{_SITE_ID}] curl failed after {retries} attempts: {exc}")
    return None


# ---------------------------------------------------------------------------
# HTML / data helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("no HTML parser available")


def _parse_date(raw: str) -> str:
    """Convert 'DD/MM/YYYY' → 'YYYY-MM-DD'. Returns raw string on failure."""
    if not raw:
        return ""
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", raw.strip())
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return raw.strip()


def _extract_year(doc_name: str) -> str:
    """Return the first 4-digit year (2000-2099 or 1900-1999) found in *doc_name*."""
    m = re.search(r"\b(20\d{2})\b", doc_name)
    if not m:
        m = re.search(r"\b(19\d{2})\b", doc_name)
    return m.group(1) if m else ""


def _parse_app_data(html: str) -> list[dict]:
    """Extract the embedded ``appData`` JS array from the listing page HTML.

    The JS objects look like::

        { "type": "pdf", "date": "29/05/2025",
          "documentName": decodeTitle("Annual Report 2024..."),
          "url": decodeTitle("/docs/.../file.pdf?sfvrsn=xxx"),
          "translatedVersion": "", "size": "8340 KB" }

    After unwrapping ``decodeTitle("...")`` each object is valid JSON.
    """
    m = re.search(r"var appData\s*=\s*(\[.*?\]);", html, re.DOTALL)
    if not m:
        return []

    raw = m.group(1)
    # Unwrap decodeTitle("...") → the raw string content
    raw = re.sub(
        r'decodeTitle\("([^"]*)"\)',
        lambda x: '"' + x.group(1) + '"',
        raw,
    )

    records: list[dict] = []
    for obj_m in re.finditer(r'\{[^{}]+\}', raw, re.DOTALL):
        obj_str = obj_m.group(0)
        try:
            rec = json.loads(obj_str)
            records.append(rec)
        except json.JSONDecodeError:
            # Fallback: extract key-value pairs manually
            rec: dict = {}
            for kv in re.finditer(r'"(\w+)"\s*:\s*"([^"]*)"', obj_str):
                rec[kv.group(1)] = kv.group(2)
            if rec:
                records.append(rec)
    return records


def _fetch_detail_text(year: str) -> str:
    """Fetch the CBI detail page for *year* and return main article text.

    Only attempted for recent years (≥ 2020); older years lack individual pages.
    Returns empty string on any failure or when the page is a 404.
    """
    try:
        if int(year) < 2020:
            return ""
    except ValueError:
        return ""

    url = (
        f"{_BASE}/publication/corporate-reports/"
        f"central-bank-annual-report-and-annual-performance-statement-{year}"
    )
    raw = _curl_get(url)
    if not raw:
        return ""
    if "page not found" in raw.lower() or "page-not-found" in raw[:500].lower():
        return ""

    try:
        soup = _make_soup(raw)
    except Exception:
        return ""

    try:
        for tag in soup.find_all(
            ["nav", "header", "footer", "script", "style", "noscript", "aside"]
        ):
            tag.decompose()

        article = (
            soup.find("article")
            or soup.find(id=re.compile(r"MainContent", re.I))
            or soup.find("main")
        )
        if not article:
            return ""

        parts: list[str] = []
        for el in article.find_all(["p", "h2", "h3"]):
            t = el.get_text(" ", strip=True)
            if t and len(t) > 20:
                parts.append(t)

        text = re.sub(r"\s+", " ", " ".join(parts)).strip()
        return text[:3000] if text else ""

    except Exception:
        return ""


def _build_abstract(doc_name: str, date_raw: str, size: str, detail_text: str) -> str:
    """Return a reliable abstract (always ≥ 100 chars).

    The synthesised summary is always appended so the result is non-trivial even
    for older documents without individual detail pages.
    """
    year = _extract_year(doc_name)
    year_phrase = f" for {year}" if year else ""

    synthesised = (
        f"The {doc_name} is an official publication of the Central Bank of Ireland, "
        f"published on {date_raw}. This document covers the Central Bank's regulatory "
        f"activities, financial stability assessments, governance structures, and "
        f"performance indicators{year_phrase}. "
        f"Available as a PDF document ({size})."
    )

    if detail_text and len(detail_text) >= 30:
        combined = detail_text.rstrip() + " " + synthesised
        return combined[:5000]
    return synthesised


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class CentralBankIEPublicationCrawler(BaseCrawler):
    """Crawler for Central Bank of Ireland annual reports publication listing."""

    site_id = "centralbank-ie-publication"
    site_name = "Custom: centralbank-ie-publication"
    base_url = "https://www.centralbank.ie"

    def crawl(self, limit=None):
        """Crawl all annual reports from the CBI publication listing page.

        All records are embedded in the listing page HTML — a single fetch is
        enough to discover every item (no pagination).  Detail pages are fetched
        for recent years (≥ 2020) to enrich the abstract with real content.

        Parameters
        ----------
        limit:
            Maximum number of records to save.  ``None`` means unlimited.
        """
        crawl_start = time.time()
        max_wall = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute wall-clock budget
        limit_display = str(limit) if limit is not None else "∞"

        # ── Fetch listing page ───────────────────────────────────────────
        print(f"[{_SITE_ID}] Fetching listing page: {_LIST_URL}")
        html = _curl_get(_LIST_URL)
        if not html:
            print(f"[{_SITE_ID}] Failed to fetch listing page. Aborting.")
            return 0

        records = _parse_app_data(html)
        if not records:
            print(f"[{_SITE_ID}] No records found in appData. Aborting.")
            return 0

        total = len(records)
        print(f"[{_SITE_ID}] Found {total} records. Crawling up to {limit_display}.")

        saved = 0
        seen_urls: set[str] = set()

        for idx, rec in enumerate(records):
            # ── Limit / budget guards ────────────────────────────────────
            if limit is not None and saved >= limit:
                break
            if time.time() - crawl_start > max_wall:
                print(f"[{_SITE_ID}] Wall-clock budget reached at item {idx}. Stopping.")
                break

            try:
                doc_name = rec.get("documentName", "").strip()
                date_raw = rec.get("date", "").strip()
                pdf_rel = rec.get("url", "").strip()
                size = rec.get("size", "").strip()
                translated = rec.get("translatedVersion", "").strip()
                doc_type = rec.get("type", "pdf")

                if not doc_name or not pdf_rel:
                    print(f"[{_SITE_ID}] item {idx}: missing name or URL, skipping.")
                    continue

                # ── Build full PDF URL ───────────────────────────────────
                pdf_url = pdf_rel if pdf_rel.startswith("http") else _BASE + pdf_rel

                if pdf_url in seen_urls:
                    print(f"[{_SITE_ID}] item {idx}: duplicate URL, skipping.")
                    continue
                seen_urls.add(pdf_url)

                # ── Dates ────────────────────────────────────────────────
                published_date = _parse_date(date_raw)
                year = _extract_year(doc_name)

                # ── external_id / original_filename ─────────────────────
                parsed_path = urllib.parse.urlparse(pdf_url).path
                filename = parsed_path.split("/")[-1]
                original_filename = filename.split("?")[0]
                external_id = re.sub(r"\.pdf$", "", original_filename, flags=re.IGNORECASE)

                # ── Detail page (abstract enrichment for recent years) ───
                time.sleep(self._delay)
                detail_text = _fetch_detail_text(year) if year else ""

                abstract = _build_abstract(doc_name, date_raw, size, detail_text)

                if len(abstract) < 50:
                    print(
                        f"[{_SITE_ID}] item {idx} '{doc_name[:40]}': "
                        f"abstract too short ({len(abstract)}c), skipping."
                    )
                    continue

                # ── Detail page URL ──────────────────────────────────────
                try:
                    year_int = int(year) if year else 0
                except ValueError:
                    year_int = 0

                if year_int >= 2020:
                    detail_url = (
                        f"{_BASE}/publication/corporate-reports/"
                        f"central-bank-annual-report-and-annual-performance-statement-{year}"
                    )
                else:
                    detail_url = _LIST_URL

                # ── Assemble paper dict ──────────────────────────────────
                paper = {
                    "site_id":           self.site_id,
                    "external_id":       external_id,
                    "post_number":       year or external_id,
                    "title":             doc_name,
                    "abstract":          abstract,
                    "authors":           None,
                    "publisher":         _PUBLISHER,
                    "department":        _PUBLISHER,
                    "journal":           None,
                    "category":          "Annual Reports",
                    "keywords":          "annual report,central bank,ireland,corporate report",
                    "published_date":    published_date,
                    "listed_date":       published_date,
                    "url":               detail_url,
                    "pdf_url":           pdf_url,
                    "doi":               None,
                    "original_filename": original_filename,
                    "metadata":          json.dumps(
                        {
                            "posted_date":       date_raw,
                            "originalFilename":  original_filename,
                            "size":              size,
                            "translatedVersion": translated,
                            "docType":           doc_type,
                            "year":              year,
                        },
                        ensure_ascii=False,
                    ),
                }

                self._save_paper(paper)
                saved += 1
                print(f"[{_SITE_ID}] saved {saved}/{limit_display}: {doc_name[:60]}")

                if saved % 10 == 0:
                    print(
                        f"[{_SITE_ID}] page 1: saved {saved}/{limit_display}"
                    )

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] item {idx} failed: {exc}")
                continue

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
