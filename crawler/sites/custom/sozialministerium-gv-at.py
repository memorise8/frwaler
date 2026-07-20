# -*- coding: utf-8 -*-
"""Crawler for sozialministerium.gv.at — Austrian Federal Ministry of Social Affairs.

Discovers PDF documents via the site's full-text search API and extracts
content via pdftotext/pdfinfo.  No detail page exists for DAM assets, so
the PDF URL serves as both url and pdf_url.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from crawler.base_crawler import BaseCrawler

_SITE_ID = "sozialministerium-gv-at"
_SEARCH_URL = "https://www.sozialministerium.gv.at/.search"
_PAGE_SIZE = 10
_MAX_PAGES = 200
_RATE_SLEEP = 0.5       # seconds between page fetches
_PDF_RATE_SLEEP = 1.0   # seconds between PDF downloads
_MAX_SECONDS = 25 * 60  # 25-minute wall-clock budget


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _curl_get(url, timeout=30, retries=3):
    """Fetch URL via curl, return decoded UTF-8 text or None on failure."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "-H", "Accept-Language: de-AT,de;q=0.9,en;q=0.7",
        url,
    ]
    for attempt in range(retries):
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
            if res.returncode == 0 and res.stdout:
                return res.stdout.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[{_SITE_ID}] curl error (attempt {attempt + 1}): {exc}")
        if attempt < retries - 1:
            wait = [1, 3, 9][attempt]
            time.sleep(wait)
    return None


def _curl_download(url, dest_path, timeout=90, retries=3):
    """Download binary (PDF) to dest_path via curl. Returns True on success."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "-o", dest_path,
        url,
    ]
    for attempt in range(retries):
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
            if res.returncode == 0 and os.path.exists(dest_path) and os.path.getsize(dest_path) > 512:
                return True
        except Exception as exc:
            print(f"[{_SITE_ID}] download error (attempt {attempt + 1}): {exc}")
        if attempt < retries - 1:
            wait = [1, 3, 9][attempt]
            time.sleep(wait)
    return False


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

def _make_soup(html):
    """Parse HTML with best available parser; fallback chain html5lib→lxml→html.parser."""
    if not html:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# PDF content extraction
# ---------------------------------------------------------------------------

def _extract_pdf_data(pdf_path):
    """Extract body text and metadata from a downloaded PDF.

    Tries pdftotext with increasing page counts until >=100 chars are found.
    Returns (abstract_str, metadata_dict).
    """
    text = ""
    meta = {}

    # pdfinfo — document metadata (author, dates, page count)
    try:
        info = subprocess.run(
            ["pdfinfo", pdf_path],
            capture_output=True, text=True, timeout=15,
        )
        for line in info.stdout.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
    except Exception:
        pass

    # pdftotext — try first 2 pages, then 5, then all
    for max_pages in (2, 5, 0):
        try:
            cmd = ["pdftotext"]
            if max_pages > 0:
                cmd += ["-l", str(max_pages)]
            cmd += [pdf_path, "-"]
            res = subprocess.run(cmd, capture_output=True, timeout=30)
            if res.returncode == 0 and res.stdout:
                raw = res.stdout.decode("utf-8", errors="replace")
                candidate = " ".join(raw.split())  # normalize whitespace
                if len(candidate) >= 100:
                    text = candidate
                    break
        except Exception:
            pass

    return text, meta


def _parse_pdf_date(raw_date):
    """Convert pdfinfo CreationDate string to YYYY-MM-DD; returns None on failure.

    Input examples:
      'Fri Sep 16 21:24:18 2016 KST'
      'Mon Jan  3 12:00:00 2022 CET'
    """
    if not raw_date:
        return None
    months = {
        "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
        "May": "05", "Jun": "06", "Jul": "07", "Aug": "08",
        "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
    }
    m = re.search(r"(\w{3})\s+(\d{1,2})\s+\d{2}:\d{2}:\d{2}\s+(\d{4})", raw_date)
    if m:
        mon = months.get(m.group(1), "01")
        day = m.group(2).zfill(2)
        return f"{m.group(3)}-{mon}-{day}"
    # Fallback: just the year
    m = re.search(r"(\d{4})", raw_date)
    if m:
        return f"{m.group(1)}-01-01"
    return None


def _url_key(url):
    """Scheme-normalised URL key for dedup (lowercases only the scheme part)."""
    for prefix in ("HTTPS://", "HTTP://", "https://", "http://"):
        if url.startswith(prefix):
            return "https://" + url[len(prefix):]
    return url.lower()


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class SozialministeriumGvAtCrawler(BaseCrawler):
    """Crawler for sozialministerium.gv.at — Austrian Social Affairs Ministry PDFs."""

    site_id = "sozialministerium-gv-at"
    site_name = "Custom: sozialministerium-gv-at"
    base_url = "https://www.sozialministerium.gv.at"

    def crawl(self, limit=None):
        """Crawl PDF search results, download and extract each document.

        Pages through /.search?words=Statistik&mimetype=application_pdf until
        ``limit`` is reached, all pages exhausted, or the 25-minute budget runs out.

        Returns the number of documents saved.
        """
        start_time = time.time()
        saved = 0
        page = 1
        seen_urls = set()   # dedup across pages (normalised URL)
        limit_str = str(limit) if limit is not None else "∞"

        while page <= _MAX_PAGES:
            # Wall-clock budget
            if time.time() - start_time > _MAX_SECONDS:
                print(f"[{_SITE_ID}] 25-minute budget reached at page {page}, stopping.")
                break

            if limit is not None and saved >= limit:
                break

            # Fetch search-results HTML fragment
            params = {
                "words": "Statistik",
                "type": "simple",
                "site": "sozialministeriumat",
                "mimetype": "application_pdf",
                "page": str(page),
                "pagesize": str(_PAGE_SIZE),
                "lang": "de",
            }
            search_url = _SEARCH_URL + "?" + urlencode(params)
            html = _curl_get(search_url)

            if not html:
                print(f"[{_SITE_ID}] page {page}: failed to fetch, stopping.")
                break

            soup = _make_soup(html)
            if not soup:
                print(f"[{_SITE_ID}] page {page}: HTML parse failed, stopping.")
                break

            items = soup.select("li.overview-item")
            if not items:
                print(f"[{_SITE_ID}] page {page}: no items — end of results.")
                break

            # Filter out already-seen URLs (protects against silent pager loop-back)
            new_items = []
            for item in items:
                link = item.select_one("a.card-link")
                if not link:
                    continue
                raw_url = link.get("href", "").strip()
                if not raw_url:
                    continue
                key = _url_key(raw_url)
                if key not in seen_urls:
                    seen_urls.add(key)
                    new_items.append((raw_url, item))

            if not new_items:
                print(f"[{_SITE_ID}] page {page}: all items already seen (loop), stopping.")
                break

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_str}")

            # ----------------------------------------------------------------
            # Per-item: download PDF → extract text → save
            # ----------------------------------------------------------------
            for raw_url, item in new_items:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > _MAX_SECONDS:
                    break

                try:
                    # Title from search result card
                    title_el = item.select_one(".col-12.col-md-9")
                    title = title_el.get_text(strip=True) if title_el else ""
                    if not title:
                        h2 = item.select_one(".card-title-heading h2")
                        if h2:
                            badge = h2.select_one(".badge")
                            if badge:
                                badge.decompose()
                            title = h2.get_text(strip=True)
                    if not title:
                        # Derive from filename as last resort
                        basename = os.path.basename(urlparse(raw_url).path)
                        title = re.sub(r"\.[a-zA-Z]{2,4}$", "", basename).replace("-", " ").replace("_", " ")

                    # Download PDF to a temp file
                    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                        tmp_path = tf.name

                    abstract = ""
                    pdf_meta = {}
                    try:
                        if _curl_download(raw_url, tmp_path):
                            abstract, pdf_meta = _extract_pdf_data(tmp_path)
                            abstract = abstract[:800]  # cap abstract length
                        else:
                            print(f"[{_SITE_ID}] download failed: {raw_url}")
                    finally:
                        try:
                            os.unlink(tmp_path)
                        except Exception:
                            pass

                    if len(abstract) < 100:
                        print(
                            f"[{_SITE_ID}] abstract too short "
                            f"({len(abstract)} chars), skipping: {title[:60]}"
                        )
                        continue

                    # Metadata from pdfinfo
                    published_date = _parse_pdf_date(pdf_meta.get("CreationDate", ""))
                    author = pdf_meta.get("Author") or None

                    # external_id = URL path (unique, stable)
                    url_path = urlparse(raw_url).path
                    external_id = url_path.lstrip("/")

                    # Category from path: /dam/sozialministeriumat/Anlagen/Themen/CATEGORY/...
                    path_parts = [p for p in url_path.split("/") if p]
                    # Indices:          0    1                    2       3       4
                    category = path_parts[4] if len(path_parts) > 4 else None

                    pdf_filename = os.path.basename(url_path)

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": None,
                        "title": title,
                        "abstract": abstract,
                        "url": raw_url,
                        "pdf_url": raw_url,
                        "published_date": published_date,
                        "listed_date": None,
                        "authors": author,
                        "publisher": (
                            "Bundesministerium für Soziales, Gesundheit, "
                            "Pflege und Konsumentenschutz"
                        ),
                        "department": None,
                        "journal": None,
                        "keywords": None,
                        "category": category,
                        "doi": None,
                        "original_filename": pdf_filename,
                        "metadata": json.dumps(
                            {
                                "pdf_creator": pdf_meta.get("Creator", ""),
                                "pdf_producer": pdf_meta.get("Producer", ""),
                                "pdf_pages": pdf_meta.get("Pages", ""),
                                "pdf_creation_date": pdf_meta.get("CreationDate", ""),
                                "url_path": url_path,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item failed: {exc}")
                    continue

                time.sleep(_PDF_RATE_SLEEP)

            page += 1
            time.sleep(_RATE_SLEEP)

        if page > _MAX_PAGES:
            print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached, stopping.")

        print(f"[{_SITE_ID}] crawl complete: {saved} documents saved.")
        return saved
