# -*- coding: utf-8 -*-
"""Crawler for Nikhef Technical Reports.

Source: https://www.nikhef.nl/pub/services/newbiblio/reports.php
Single static HTML page (~30 ETR-series technical reports, 1996-2012).
No detail pages — abstract text is extracted from each PDF via pdfplumber.
"""

import io
import json
import os
import re
import subprocess
import sys
import time
from os.path import basename

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup, NavigableString, Tag
    _BS_PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    BeautifulSoup = None
    NavigableString = None
    Tag = None
    _BS_PARSERS = []

_LIST_URL = "https://www.nikhef.nl/pub/services/newbiblio/reports.php"
_PUBLISHER = "Nikhef"
_MONTH_MAP = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


def _parse_date(raw):
    """Convert 'Sep. 2012' → '2012-09'."""
    if not raw:
        return None
    raw = raw.strip()
    m = re.match(r"([A-Za-z]+)\.?\s+(\d{4})", raw)
    if m:
        mon = m.group(1).lower()[:3]
        year = m.group(2)
        return f"{year}-{_MONTH_MAP.get(mon, '01')}"
    m = re.match(r"(\d{4})", raw)
    if m:
        return m.group(1)
    return None


def _make_soup(html_bytes):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    if BeautifulSoup is None:
        return None
    if isinstance(html_bytes, bytes):
        try:
            text = html_bytes.decode("iso-8859-1", errors="replace")
        except Exception:
            text = html_bytes.decode("utf-8", errors="replace")
    else:
        text = html_bytes
    for parser in _BS_PARSERS:
        try:
            return BeautifulSoup(text, parser)
        except Exception:
            continue
    return None


def _curl_get(url, retries=3):
    """Fetch URL bytes via curl with exponential backoff (1s, 3s, 9s)."""
    delays = [1, 3, 9]
    for attempt in range(retries):
        try:
            result = subprocess.run(
                ["curl", "--tls-max", "1.3", "-sk", "-L",
                 "--max-time", "30",
                 "-H", "User-Agent: Mozilla/5.0 (compatible; crawler/1.0)",
                 url],
                capture_output=True,
                timeout=35,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout
            print(f"[nikhef-nl-pub] curl exit {result.returncode} for {url}")
        except Exception as exc:
            print(f"[nikhef-nl-pub] curl attempt {attempt + 1} error: {exc}")
        if attempt < retries - 1:
            time.sleep(delays[attempt])
    return None


def _extract_pdf_abstract(pdf_bytes):
    """Extract abstract text from PDF bytes. Returns str or None."""
    if not pdf_bytes or len(pdf_bytes) < 500:
        return None

    full_text = ""

    # Primary: pdfplumber (best text extraction from structured PDFs)
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            parts = []
            for page in pdf.pages[:4]:
                try:
                    pt = page.extract_text() or ""
                    parts.append(pt)
                except Exception:
                    pass
            full_text = "\n".join(parts)
    except Exception:
        pass

    # Fallback: pdfminer
    if not full_text.strip():
        try:
            from pdfminer.high_level import extract_text
            full_text = extract_text(io.BytesIO(pdf_bytes)) or ""
        except Exception:
            pass

    if not full_text.strip():
        return None

    return _find_best_abstract(full_text)


def _find_best_abstract(text):
    """Find the best abstract-like block in extracted PDF text."""
    # Strategy 1: Look for labeled abstract/summary section
    for label in ("abstract", "summary"):
        m = re.search(
            r"(?im)^\s*" + label + r"[\s:]*\n?(.*?)(?=\n\s*\n|\n\s*[A-Z][A-Z\s]{5,}|\Z)",
            text, re.DOTALL,
        )
        if not m:
            m = re.search(
                r"(?i)\b" + label + r"[\s:—\-]+(.*?)(?=\n\s*\n|\Z)",
                text, re.DOTALL,
            )
        if m:
            candidate = re.sub(r"\s+", " ", m.group(1)).strip()
            if 100 <= len(candidate) <= 3000:
                return candidate[:2000]

    # Strategy 2: First substantial paragraph (skip all-caps headers)
    for para in re.split(r"\n{2,}", text.strip()):
        clean = re.sub(r"\s+", " ", para).strip()
        has_lower = sum(1 for c in clean if c.islower()) > 20
        if len(clean) >= 150 and has_lower and not re.match(r"^[A-Z\s\d]{20,}$", clean):
            return clean[:2000]

    # Strategy 3: First 1500 chars if substantial
    clean = re.sub(r"\s+", " ", text[:1500]).strip()
    return clean[:2000] if len(clean) >= 100 else None


def _get_direct_text(elem):
    """Return the text directly inside elem, skipping nested <dd> children.

    lxml parses this page's <dd> elements as nested (each <dd> wraps the next),
    producing cumulative get_text() results. This function extracts ONLY the
    direct NavigableString content and inline-element text of the given node.
    Works identically for html5lib's flat structure (no nested <dd>).
    """
    if NavigableString is None or Tag is None:
        return elem.get_text(" ", strip=True)
    parts = []
    for child in elem.children:
        if isinstance(child, NavigableString):
            t = str(child).strip()
            if t:
                parts.append(t)
        elif isinstance(child, Tag) and child.name != "dd":
            # Include text from inline elements (<a>, <em>, etc.) but not nested <dd>
            t = child.get_text(" ", strip=True)
            if t:
                parts.append(t)
    result = " ".join(parts).strip()
    # Fallback: should not be needed in practice
    return result if result else elem.get_text(" ", strip=True)


def _parse_entries(soup):
    """Parse all report entries from the list page.

    Returns list of dicts: external_id, url, pdf_url, title, description, authors, published_date.
    """
    entries = []
    main_div = soup.find("div", id="main") or soup
    all_nodes = list(main_div.find_all(["dt", "dd"]))

    i = 0
    while i < len(all_nodes):
        if all_nodes[i].name != "dt":
            i += 1
            continue

        # Group consecutive <dt> elements (multiple links for same entry, e.g. PDF + bookmarked)
        dt_links = []
        while i < len(all_nodes) and all_nodes[i].name == "dt":
            for a in all_nodes[i].find_all("a", href=True):
                dt_links.append(a)
            i += 1

        # Collect following <dd> elements
        dds = []
        while i < len(all_nodes) and all_nodes[i].name == "dd":
            dds.append(all_nodes[i])
            i += 1

        if not dt_links:
            continue

        # Find primary PDF link and clean up report ID
        pdf_url = None
        report_id = None
        for a in dt_links:
            href = a.get("href", "")
            if ".pdf" in href.lower():
                pdf_url = href
                raw_text = a.get_text(strip=True)
                # "RA-M21 PDF (1,4 Mb)" → "RA-M21"
                clean_id = re.sub(
                    r"\s+(PDF|gzipped|Postscript|bookmarked).*$",
                    "", raw_text, flags=re.IGNORECASE,
                ).strip()
                report_id = clean_id or basename(href.split("?")[0]).rsplit(".", 1)[0]
                break

        if not pdf_url and dt_links:
            href = dt_links[0].get("href", "")
            pdf_url = href or None
            report_id = dt_links[0].get_text(strip=True) or f"entry-{len(entries)}"

        if not report_id:
            continue

        # Extract per-dd direct text to handle lxml's nested-dd structure
        dd_direct = [_get_direct_text(dd) for dd in dds]
        dd_direct = [t for t in dd_direct if t]

        authors = dd_direct[0] if len(dd_direct) > 0 else None
        description = dd_direct[1] if len(dd_direct) > 1 else None
        date_raw = dd_direct[2] if len(dd_direct) > 2 else None

        # Validate date field (must contain a 4-digit year)
        if date_raw and not re.search(r"\d{4}", date_raw):
            date_raw = None

        entries.append({
            "external_id": report_id,
            "url": pdf_url or _LIST_URL,
            "pdf_url": pdf_url,
            "title": description or report_id,
            "description": description or "",
            "authors": authors,
            "published_date": _parse_date(date_raw),
        })

    return entries


class NikhefPubCrawler(BaseCrawler):
    site_id = "nikhef-nl-pub"
    site_name = "Custom: nikhef-nl-pub"
    base_url = "https://www.nikhef.nl"

    def crawl(self, limit=None):
        start_time = time.time()
        max_seconds = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute wall-clock budget

        saved = 0
        seen_urls = set()
        limit_or_inf = limit if limit is not None else float("inf")

        print(f"[nikhef-nl-pub] Fetching {_LIST_URL}")
        raw = _curl_get(_LIST_URL)
        if not raw:
            print("[nikhef-nl-pub] Failed to fetch list page")
            return 0

        soup = _make_soup(raw)
        if not soup:
            print("[nikhef-nl-pub] Failed to parse HTML")
            return 0

        entries = _parse_entries(soup)
        print(f"[nikhef-nl-pub] Found {len(entries)} report entries on list page")

        for idx, entry in enumerate(entries):
            if saved >= limit_or_inf:
                break
            if time.time() - start_time > max_seconds:
                print(f"[nikhef-nl-pub] Wall-clock budget reached after {saved} saved")
                break

            ext_id = entry.get("external_id", f"item-{idx}")
            pdf_url = entry.get("pdf_url")
            url = entry.get("url") or pdf_url or _LIST_URL

            if url in seen_urls:
                continue
            seen_urls.add(url)

            try:
                abstract = self._get_abstract(entry)

                if not abstract or len(abstract) < 50:
                    print(f"[nikhef-nl-pub] {ext_id}: abstract {len(abstract or '')} chars (<50), skipping")
                    continue
                if len(abstract) < 100:
                    print(f"[nikhef-nl-pub] {ext_id}: abstract {len(abstract)} chars (<100), skipping")
                    continue

                original_filename = (
                    basename(pdf_url.rstrip("/").split("?")[0]) if pdf_url else None
                )
                date_str = entry.get("published_date")

                paper = {
                    "site_id": self.site_id,
                    "external_id": ext_id,
                    "post_number": ext_id,
                    "title": entry.get("title", "(untitled)"),
                    "abstract": abstract,
                    "published_date": date_str,
                    "posted_date": date_str,
                    "listed_date": date_str,
                    "authors": entry.get("authors"),
                    "publisher": _PUBLISHER,
                    "department": None,
                    "journal": None,
                    "url": url,
                    "pdf_url": pdf_url,
                    "keywords": None,
                    "category": "technical-report",
                    "doi": None,
                    "original_filename": original_filename,
                    "metadata": json.dumps({
                        "posted_date": date_str,
                        "originalFilename": original_filename,
                        "report_id": ext_id,
                        "category": "technical-report",
                    }),
                }

                self._save_paper(paper)
                saved += 1
                print(
                    f"[nikhef-nl-pub] saved {saved}/{limit_or_inf}: "
                    f"[{ext_id}] {entry.get('title', '')[:55]} ({len(abstract)} chars)"
                )

                if (idx + 1) % 10 == 0:
                    print(f"[nikhef-nl-pub] page 1: saved {saved}/{limit_or_inf}")

                if saved < limit_or_inf:
                    time.sleep(self._delay)

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[nikhef-nl-pub] item {ext_id} failed: {exc}")
                continue

        print(f"[nikhef-nl-pub] Done. Saved {saved} total.")
        return saved

    def _get_abstract(self, entry):
        """Get abstract: try PDF text extraction, fall back to description if >= 100 chars."""
        pdf_url = entry.get("pdf_url")
        description = entry.get("description", "")
        abstract = None

        if pdf_url:
            try:
                pdf_bytes = _curl_get(pdf_url, retries=3)
                if pdf_bytes:
                    abstract = _extract_pdf_abstract(pdf_bytes)
            except Exception as exc:
                print(f"[nikhef-nl-pub] PDF extract error for {pdf_url}: {exc}")

        # Fall back to description if PDF extraction failed or gave short result
        if (not abstract or len(abstract) < 100) and description and len(description) >= 100:
            abstract = description

        return abstract
