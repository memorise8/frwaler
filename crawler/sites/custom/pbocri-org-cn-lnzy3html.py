# -*- coding: utf-8 -*-
"""Crawler for PBOCRI 历年摘要 (Annual Yearbook Abstracts).

Starting URL: https://www.pbocri.org.cn/lnzy3.html
Publishes: 中国金融年鉴 (China Financial Yearbook) annual summaries as PDFs.
Strategy: parse static HTML list → download each PDF → extract text via
pdftotext → use extracted text as abstract.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))
from crawler.base_crawler import BaseCrawler

_SITE_ID = "pbocri-org-cn-lnzy3html"
_LIST_URL = "https://www.pbocri.org.cn/lnzy3.html"
_RATE_SLEEP = 1.0
_MAX_PAGES = 200  # safety cap (this site is a single page, kept for spec)
_CRAWL_BUDGET_SECS = 25 * 60


def _curl(url, out_path=None, retries=3):
    """Fetch URL with curl (TLS-tolerant). Returns bytes or None on failure."""
    delay = 1
    for attempt in range(retries):
        try:
            if out_path:
                cmd = ["curl", "--tls-max", "1.3", "-sk", "-L",
                       "--max-time", "60", "-o", out_path, url]
            else:
                cmd = ["curl", "--tls-max", "1.3", "-sk", "-L",
                       "--max-time", "60", url]
            result = subprocess.run(cmd, capture_output=True, timeout=90)
            if result.returncode == 0:
                return result.stdout if not out_path else b""
            print(f"[{_SITE_ID}] curl failed (rc={result.returncode}) attempt {attempt+1}/{retries}: {url}")
        except Exception as exc:
            print(f"[{_SITE_ID}] curl error attempt {attempt+1}/{retries}: {exc}")
        if attempt < retries - 1:
            time.sleep(delay)
            delay *= 3
    return None


def _parse_html(raw):
    """Parse raw bytes/str into BeautifulSoup with fallback chain."""
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            text = raw.decode("latin-1", errors="replace")
    else:
        text = raw
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(text, parser)
        except Exception:
            continue
    return None


def _extract_pdf_text(pdf_path):
    """Extract text from a PDF file using pdftotext. Returns str."""
    try:
        result = subprocess.run(
            ["pdftotext", "-enc", "UTF-8", pdf_path, "-"],
            capture_output=True, timeout=60
        )
        if result.returncode == 0:
            return result.stdout.decode("utf-8", errors="replace")
    except Exception as exc:
        print(f"[{_SITE_ID}] pdftotext failed: {exc}")
    # Fallback: try pypdf
    try:
        import pypdf
        reader = pypdf.PdfReader(pdf_path)
        parts = []
        for page in reader.pages[:10]:
            parts.append(page.extract_text() or "")
        return "\n".join(parts)
    except Exception:
        pass
    # Fallback: try pdfplumber
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            parts = []
            for page in pdf.pages[:10]:
                t = page.extract_text()
                if t:
                    parts.append(t)
        return "\n".join(parts)
    except Exception:
        pass
    return ""


def _clean_text(raw_text):
    """Clean extracted PDF text for use as abstract."""
    # Remove lines that are just dots and numbers (TOC entries like "·xxx … (12)")
    lines = raw_text.splitlines()
    cleaned = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Keep lines that have real Chinese content
        cleaned.append(line)
    # Join and collapse whitespace
    text = " ".join(cleaned)
    # Remove repeated dots
    text = re.sub(r'[·…\.]{3,}', ' ', text)
    # Collapse multiple spaces
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _extract_year(title):
    """Extract year from title like '2024年中国金融年鉴'."""
    m = re.search(r'(\d{4})', title)
    return m.group(1) if m else None


class PBOCRILnzy3Crawler(BaseCrawler):
    site_id = "pbocri-org-cn-lnzy3html"
    site_name = "Custom: pbocri-org-cn-lnzy3html"
    base_url = "https://www.pbocri.org.cn"

    def crawl(self, limit=None):
        start_time = time.time()
        saved = 0
        seen_urls = set()
        limit_str = str(limit) if limit is not None else "∞"

        print(f"[{_SITE_ID}] Starting crawl (limit={limit_str})")

        # --- Fetch list page ---
        raw = _curl(_LIST_URL)
        if not raw:
            print(f"[{_SITE_ID}] Failed to fetch list page")
            return saved

        soup = _parse_html(raw)
        if not soup:
            print(f"[{_SITE_ID}] Failed to parse list page")
            return saved

        # Parse items: <h1><a href="PDF_URL">TITLE</a></h1>
        items = []
        for h1 in soup.find_all("h1"):
            a = h1.find("a", href=True)
            if not a:
                continue
            href = a.get("href", "").strip()
            title = a.get_text(strip=True)
            if not href or not title:
                continue
            if not href.lower().endswith(".pdf"):
                continue
            if not href.startswith("http"):
                href = self.base_url + href
            items.append((title, href))

        print(f"[{_SITE_ID}] Found {len(items)} PDF items on list page")

        if not items:
            print(f"[{_SITE_ID}] No items found — check HTML structure")
            return saved

        # --- Process each item ---
        for idx, (title, pdf_url) in enumerate(items):
            if limit is not None and saved >= limit:
                break

            # Wall-clock budget check
            elapsed = time.time() - start_time
            if elapsed > _CRAWL_BUDGET_SECS:
                print(f"[{_SITE_ID}] Budget exhausted after {elapsed:.0f}s — exiting cleanly")
                break

            if pdf_url in seen_urls:
                print(f"[{_SITE_ID}] Duplicate URL skipped: {pdf_url}")
                continue
            seen_urls.add(pdf_url)

            # Progress log every 10 items
            if idx > 0 and idx % 10 == 0:
                print(f"[{_SITE_ID}] page 1: saved {saved}/{limit_str}")

            try:
                abstract = self._fetch_abstract(title, pdf_url)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] item {idx+1} failed during fetch: {exc}")
                continue

            if not abstract or len(abstract) < 50:
                print(f"[{_SITE_ID}] item '{title[:40]}' abstract too short ({len(abstract) if abstract else 0} chars) — skipping")
                continue

            year = _extract_year(title)
            published_date = f"{year}-01-01" if year else None

            # Derive external_id from PDF URL hash (filename without extension)
            pdf_fname = pdf_url.split("/")[-1]
            external_id = pdf_fname.replace(".pdf", "")

            paper = {
                "site_id": self.site_id,
                "external_id": external_id,
                "title": title,
                "authors": json.dumps(["《中国金融年鉴》编委会"], ensure_ascii=False),
                "abstract": abstract,
                "category": "年鉴摘要",
                "keywords": json.dumps(["中国金融年鉴", "金融", "年鉴"], ensure_ascii=False),
                "published_date": published_date,
                "url": _LIST_URL,
                "pdf_url": pdf_url,
                "doi": None,
                "department": "中国人民银行金融研究所",
                "metadata": json.dumps({"year": year, "source_page": _LIST_URL}, ensure_ascii=False),
            }

            try:
                self._save_paper(paper)
                saved += 1
                print(f"[{_SITE_ID}] Saved ({saved}/{limit_str}): {title}")
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] item {idx+1} save failed: {exc}")
                continue

            time.sleep(_RATE_SLEEP)

        print(f"[{_SITE_ID}] Crawl complete — saved {saved} records")
        return saved

    def _fetch_abstract(self, title, pdf_url):
        """Download PDF and extract text as abstract. Returns str."""
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            data = _curl(pdf_url, out_path=tmp_path)
            if data is None:
                print(f"[{_SITE_ID}] Failed to download PDF: {pdf_url}")
                return ""

            if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) < 100:
                print(f"[{_SITE_ID}] PDF too small or missing: {tmp_path}")
                return ""

            raw_text = _extract_pdf_text(tmp_path)
            abstract = _clean_text(raw_text)

            # Truncate to a reasonable abstract length
            if len(abstract) > 3000:
                abstract = abstract[:3000]

            return abstract
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
