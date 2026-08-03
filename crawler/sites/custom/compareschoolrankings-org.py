# -*- coding: utf-8 -*-
"""Fraser Institute "Compare School Rankings" crawler (compareschoolrankings.org).

Starting URL: https://www.compareschoolrankings.org/

The site is a client-side rendered Vue SPA; static requests only return the
empty app shell, so the ``/pdfs`` listing page is fetched with a headless
browser via ``crawler.playwright_fetcher.fetch_html``. That page lists the
site's "Report Card" PDFs grouped by province. Each PDF is downloaded and
parsed with ``pypdf`` to recover a real title/author/abstract from the
document itself (the PDF's own Introduction section and document metadata),
since the listing page carries no abstract text.

The site's ``/api/v1/fraser_sr_research_data`` "related research" endpoint
(hosted on fraserinstitute.org) is blocked cross-origin/by Cloudflare even
from a real browser session, and the wider report-card archive lives behind
a Cloudflare challenge on fraserinstitute.org, so this crawler is scoped to
the PDFs directly reachable from compareschoolrankings.org.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BS
    _PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    _BS = None
    _PARSERS = []

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None


def _make_soup(raw_html):
    if _BS is None:
        raise RuntimeError("BeautifulSoup (bs4) is not installed")
    last_exc = None
    for parser in _PARSERS:
        try:
            return _BS(raw_html, parser)
        except Exception as exc:
            last_exc = exc
            continue
    raise RuntimeError(f"No working HTML parser (tried {_PARSERS}): {last_exc}")


_BRANDING_RE = re.compile(
    r"(?i)compare\s*school\s*rankings\.?\s*org|fraser\s+i\s*n\s*s\s*t\s*i\s*t\s*u\s*t\s*e"
)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_TOC_MARKER_RE = re.compile(r"/\s*\d{1,3}\b")
_CREATION_DATE_RE = re.compile(r"D:(\d{4})(\d{2})(\d{2})")


class CompareSchoolRankingsOrgCrawler(BaseCrawler):
    """Crawler for the Fraser Institute's compareschoolrankings.org report cards."""

    site_id = "compareschoolrankings-org"
    site_name = "Custom: compareschoolrankings-org"
    base_url = "https://www.compareschoolrankings.org"

    _LIST_URL = "https://www.compareschoolrankings.org/pdfs"
    _MIN_ABSTRACT = 50
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_WALL = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds
    _PUBLISHER = "Fraser Institute"

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_bytes(self, url: str):
        """GET raw bytes via curl with exponential-backoff retries. Returns bytes or None."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "60",
            "-A", self.USER_AGENT,
            url,
        ]
        waits = [1, 3, 9]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=65)
                if result.stdout:
                    return result.stdout
                if attempt < 2:
                    print(f"[{self.site_id}] empty response, retry in {waits[attempt]}s: {url}")
                    time.sleep(waits[attempt])
            except Exception as exc:
                if attempt < 2:
                    print(f"[{self.site_id}] curl error ({exc}), retry in {waits[attempt]}s: {url}")
                    time.sleep(waits[attempt])
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return None

    def _curl_last_modified(self, url: str):
        """HEAD request to recover the Last-Modified header. Returns raw string or None."""
        cmd = [
            "curl", "-skI", "--tls-max", "1.3", "--max-time", "30",
            "-A", self.USER_AGENT,
            url,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            text = result.stdout.decode("utf-8", errors="replace")
            m = re.search(r"(?im)^last-modified:\s*(.+)$", text)
            return m.group(1).strip() if m else None
        except Exception:
            return None

    def _fetch_list_html(self):
        """Render the /pdfs listing page with a headless browser (SPA, no static HTML)."""
        from crawler.playwright_fetcher import fetch_html

        waits = [1, 3, 9]
        for attempt in range(3):
            try:
                html = fetch_html(self._LIST_URL, timeout_seconds=40)
                if html and len(html) > 500:
                    return html
                if attempt < 2:
                    print(f"[{self.site_id}] short/empty render, retry in {waits[attempt]}s")
                    time.sleep(waits[attempt])
            except Exception as exc:
                if attempt < 2:
                    print(f"[{self.site_id}] playwright error ({exc}), retry in {waits[attempt]}s")
                    time.sleep(waits[attempt])
                else:
                    print(f"[{self.site_id}] playwright failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # Listing parse
    # ------------------------------------------------------------------

    def _parse_listing(self, raw_html):
        """Return list of dicts: {province, title, pdf_url}. Stops at the "Archive" section."""
        entries = []
        try:
            soup = _make_soup(raw_html)
        except Exception as exc:
            print(f"[{self.site_id}] listing HTML parse error: {exc}")
            return entries

        first_link = soup.find("a", href=re.compile(r"/pdf/.+\.pdf", re.I))
        if not first_link:
            return entries
        first_ul = first_link.find_parent("ul")
        container = first_ul.parent if first_ul else soup

        current_province = ""
        for child in container.find_all(["p", "ul", "h1", "h2", "h3"], recursive=False):
            text = child.get_text(strip=True)
            if child.name == "ul":
                if not current_province:
                    continue
                for li in child.find_all("li"):
                    link = li.find("a", href=True)
                    if not link:
                        continue
                    href = link["href"].strip()
                    if not re.search(r"/pdf/.+\.pdf", href, re.I):
                        continue
                    title = li.get_text(" ", strip=True)
                    title = re.sub(r"\s*\([^)]*\)\s*$", "", title).strip()
                    if href.startswith("http"):
                        pdf_url = href
                    else:
                        pdf_url = self.base_url + href if href.startswith("/") else f"{self.base_url}/{href}"
                    entries.append({
                        "province": current_province,
                        "title": title,
                        "pdf_url": pdf_url,
                    })
            else:
                if text.lower() == "archive":
                    break
                if text:
                    current_province = text

        return entries

    # ------------------------------------------------------------------
    # PDF parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_authors(page0_text: str) -> str:
        text = re.sub(r"\s+", " ", page0_text or "").strip()
        years = list(_YEAR_RE.finditer(text))
        if not years:
            return ""
        segment = text[years[-1].end():]
        segment = _BRANDING_RE.sub("", segment)
        segment = segment.strip(" .’'\"")
        parts = re.split(r",|\s+and\s+|\s+et\s+", segment)
        parts = [p.strip() for p in parts if p.strip() and len(p.strip()) < 60]
        return "; ".join(parts)

    @staticmethod
    def _extract_intro_abstract(reader, max_pages=10) -> str:
        for i in range(min(max_pages, len(reader.pages))):
            try:
                text = reader.pages[i].extract_text() or ""
            except Exception:
                continue
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) < 300:
                continue
            if len(_TOC_MARKER_RE.findall(text)) >= 4:
                continue
            return text
        return ""

    @staticmethod
    def _pdf_creation_date(meta) -> str:
        raw = (meta or {}).get("/CreationDate") or ""
        m = _CREATION_DATE_RE.search(raw)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        return ""

    @staticmethod
    def _detect_level(title: str) -> str:
        low = title.lower()
        if "elementary" in low:
            return "Elementary Schools"
        if "secondary" in low or "secondaires" in low:
            return "Secondary Schools"
        if "high school" in low:
            return "High Schools"
        return ""

    def _parse_pdf(self, pdf_bytes: bytes) -> dict:
        if PdfReader is None:
            raise RuntimeError("pypdf is not installed")
        reader = PdfReader(io.BytesIO(pdf_bytes))
        meta = reader.metadata or {}

        page0_text = ""
        try:
            page0_text = reader.pages[0].extract_text() or ""
        except Exception:
            pass

        authors = self._extract_authors(page0_text)

        subject = (meta.get("/Subject") or "").strip()
        if len(subject) >= 100:
            abstract = re.sub(r"\s+", " ", subject).strip()
        else:
            abstract = self._extract_intro_abstract(reader)

        published_date = self._pdf_creation_date(meta)

        return {
            "authors": authors,
            "abstract": abstract,
            "published_date": published_date,
            "pdf_title": (meta.get("/Title") or "").strip(),
            "creation_date_raw": (meta.get("/CreationDate") or ""),
            "num_pages": len(reader.pages),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        page_num = 0
        lim_str = str(limit) if limit is not None else "inf"

        try:
            raw_html = self._fetch_list_html()
            if not raw_html:
                print(f"[{self.site_id}] Failed to render listing page. Aborting.")
                return 0

            entries = self._parse_listing(raw_html)
            if not entries:
                print(f"[{self.site_id}] No report-card entries found on listing page. Aborting.")
                return 0

            print(f"[{self.site_id}] Found {len(entries)} report-card PDFs across provinces.")

            page_num = 1
            new_on_page = 0

            for entry in entries:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > self._MAX_WALL:
                    print(f"[{self.site_id}] Wall-clock budget exceeded. Stopping cleanly.")
                    break

                pdf_url = entry["pdf_url"]
                if pdf_url in seen_urls:
                    continue
                seen_urls.add(pdf_url)
                new_on_page += 1

                try:
                    title = entry["title"]
                    if not title:
                        print(f"[{self.site_id}] Empty title for {pdf_url}, skipping.")
                        continue

                    time.sleep(self._delay)
                    pdf_bytes = self._curl_bytes(pdf_url)
                    if not pdf_bytes:
                        print(f"[{self.site_id}] Failed to download {pdf_url} after retries. Skipping.")
                        continue

                    parsed = self._parse_pdf(pdf_bytes)

                    abstract = parsed["abstract"]
                    if len(abstract) < self._MIN_ABSTRACT:
                        print(f"[{self.site_id}] Short abstract ({len(abstract)}) for '{title[:50]}', skipping.")
                        continue

                    province = entry["province"]
                    level = self._detect_level(title)
                    category = f"{province} - {level}" if level else province
                    keywords = ", ".join(p for p in [province, level, "School Report Card"] if p)

                    published_date = parsed["published_date"]
                    if not published_date:
                        year_match = list(_YEAR_RE.finditer(title))
                        published_date = f"{year_match[-1].group(0)}-01-01" if year_match else None
                    listed_date = published_date

                    last_modified_raw = self._curl_last_modified(pdf_url)

                    slug_match = re.search(r"/pdf/([^/]+)\.pdf", pdf_url, re.I)
                    slug = slug_match.group(1) if slug_match else pdf_url
                    original_filename = f"{slug}.pdf"

                    meta_dict = {
                        "posted_date": listed_date,
                        "http_last_modified": last_modified_raw,
                        "originalFilename": original_filename,
                        "province": province,
                        "school_level": level,
                        "pdf_slug": slug,
                        "creation_date_raw": parsed["creation_date_raw"],
                        "pdf_title_metadata": parsed["pdf_title"],
                        "num_pages": parsed["num_pages"],
                        "list_page_url": self._LIST_URL,
                    }

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": slug,
                        "post_number": slug,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "authors": parsed["authors"],
                        "publisher": self._PUBLISHER,
                        "department": "",
                        "journal": "",
                        "url": pdf_url,
                        "pdf_url": pdf_url,
                        "keywords": keywords,
                        "category": category,
                        "doi": None,
                        "original_filename": original_filename,
                        "metadata": json.dumps(meta_dict, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{lim_str}: {title[:70]}")

                except Exception as exc:
                    print(f"[{self.site_id}] item failed (url={pdf_url}): {exc}; continuing.")
                    continue

            if page_num % 10 == 0:
                print(f"[{self.site_id}] page {page_num}: saved {saved}/{lim_str}")

            if new_on_page == 0:
                print(f"[{self.site_id}] No new records found. Ending crawl (single static listing page).")

            if page_num >= self._MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached.")

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved {saved} so far.")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
