# -*- coding: utf-8 -*-
"""Crawler for CELLS/ALBA Synchrotron corporate publications.

Starting URL: https://www.cells.es/en/public/corporate-publications
Site is Plone 6 + Volto SSR — no public REST API; we parse the SSR HTML
and extract abstract text from each publication's PDF.
"""

import json
import os
import re
import subprocess
import tempfile
import time

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BS
    _DEFAULT_PARSER = "html5lib"
except ImportError:  # pragma: no cover
    _BS = None
    _DEFAULT_PARSER = "html.parser"

_LIST_URL = "https://www.cells.es/en/public/corporate-publications"
_PUBLISHER = "ALBA Synchrotron - CELLS"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MAX_WALL_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))


class CellsEsEnCrawler(BaseCrawler):
    site_id = "cells-es-en"
    site_name = "Custom: cells-es-en"
    base_url = "https://www.cells.es"

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, retries=3, timeout=60):
        """Fetch URL bytes via curl with exponential backoff retries."""
        cmd = [
            "curl", "-sk", "--tls-max", "1.3",
            "--max-time", str(timeout),
            "-A", self.USER_AGENT,
            "-L", url,
        ]
        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
                if result.returncode == 0 and result.stdout:
                    return result.stdout
                if attempt < retries - 1:
                    wait = (attempt + 1) ** 2
                    print(f"[{self.site_id}] empty response attempt {attempt+1}, retry in {wait}s")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < retries - 1:
                    wait = (attempt + 1) ** 2
                    print(f"[{self.site_id}] curl error attempt {attempt+1}: {exc}, retry in {wait}s")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after {retries} attempts for {url}: {exc}")
        return None

    def _make_soup(self, raw):
        """Parse HTML bytes/str with html5lib → lxml → html.parser fallback."""
        if _BS is None:
            return None
        html = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        for parser in ["html5lib", "lxml", "html.parser"]:
            try:
                return _BS(html, parser)
            except Exception:
                continue
        return None

    def _extract_pdf_text(self, pdf_url, max_chars=800):
        """Download PDF, extract text from first 3 pages via pdftotext (+ pdfplumber fallback).

        Returns text string (>= 50 chars) or None.
        """
        tmpfile = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
                tmpfile = f.name

            # Download PDF with retry
            dl_cmd = [
                "curl", "-sk", "--tls-max", "1.3", "--max-time", "90",
                "-A", self.USER_AGENT,
                "-L", "-o", tmpfile,
                pdf_url,
            ]
            downloaded = False
            for attempt in range(3):
                try:
                    r = subprocess.run(dl_cmd, capture_output=True, timeout=95)
                    if r.returncode == 0 and os.path.exists(tmpfile) and os.path.getsize(tmpfile) > 1000:
                        downloaded = True
                        break
                except Exception:
                    pass
                if attempt < 2:
                    time.sleep((attempt + 1) * 3)
            if not downloaded:
                print(f"[{self.site_id}] PDF download failed: {pdf_url}")
                return None

            # Primary: pdftotext
            try:
                txt = subprocess.run(
                    ["pdftotext", "-f", "1", "-l", "3", tmpfile, "-"],
                    capture_output=True, timeout=30,
                )
                if txt.returncode == 0:
                    text = txt.stdout.decode("utf-8", errors="replace")
                    text = re.sub(r"\s+", " ", text).strip()
                    if len(text) >= 50:
                        return text[:max_chars]
            except Exception:
                pass

            # Fallback: pdfplumber
            try:
                import pdfplumber
                with pdfplumber.open(tmpfile) as pdf:
                    parts = []
                    for page in pdf.pages[:3]:
                        parts.append(page.extract_text() or "")
                    text = re.sub(r"\s+", " ", " ".join(parts)).strip()
                    if len(text) >= 50:
                        return text[:max_chars]
            except Exception:
                pass

            return None

        except Exception as exc:
            print(f"[{self.site_id}] PDF extraction error for {pdf_url}: {exc}")
            return None
        finally:
            if tmpfile and os.path.exists(tmpfile):
                try:
                    os.unlink(tmpfile)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl ALBA Synchrotron corporate publications.

        The page is a single SSR HTML page listing annual Activity Reports
        (2013–present). Each report has a title and a PDF download link.
        Abstracts are extracted from the first 3 pages of each PDF.
        """
        saved = 0
        seen_urls = set()   # tracks pdf_url strings already processed
        limit_or_inf = limit if limit is not None else float("inf")
        start_time = time.time()

        for page_num in range(1, _MAX_PAGES + 1):
            if saved >= limit_or_inf:
                break
            if time.time() - start_time > _MAX_WALL_SECS:
                print(f"[{self.site_id}] wall-clock budget exceeded at page {page_num}, stopping")
                break
            if page_num == _MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached, stopping")

            if page_num % 10 == 0:
                print(f"[{self.site_id}] page {page_num}: saved {saved}/{limit_or_inf}")

            # This site has only one listing page.
            if page_num > 1:
                break

            raw = self._curl_get(_LIST_URL)
            if not raw:
                print(f"[{self.site_id}] failed to fetch listing page")
                break

            soup = self._make_soup(raw)
            if not soup:
                print(f"[{self.site_id}] failed to parse listing HTML")
                break

            # Collect h3 publication titles (anchor_id, title_text)
            entries = []
            for h in soup.find_all("h3"):
                anchor = h.get("id", "")
                title_text = h.get_text(strip=True)
                if title_text:
                    entries.append((anchor, title_text))

            # Collect PDF download hrefs (ordered, no duplicates)
            pdf_hrefs = []
            seen_hrefs = set()
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "@@download/file" in href and href not in seen_hrefs:
                    pdf_hrefs.append(href)
                    seen_hrefs.add(href)

            print(f"[{self.site_id}] page {page_num}: found {len(entries)} titles, {len(pdf_hrefs)} PDFs")

            if not entries or not pdf_hrefs:
                print(f"[{self.site_id}] no items found on page {page_num}, stopping")
                break

            # Pair titles with PDF links (by position)
            pairs = list(zip(entries, pdf_hrefs))

            for idx, ((anchor, title), pdf_path) in enumerate(pairs):
                if saved >= limit_or_inf:
                    break
                if time.time() - start_time > _MAX_WALL_SECS:
                    print(f"[{self.site_id}] wall-clock budget exceeded during items, stopping")
                    break

                try:
                    # Resolve absolute URL
                    pdf_url = (
                        pdf_path if pdf_path.startswith("http")
                        else self.base_url + pdf_path
                    )

                    # URL deduplication
                    if pdf_url in seen_urls:
                        continue
                    seen_urls.add(pdf_url)

                    # Year and date from title
                    year_m = re.search(r"(\d{4})", title)
                    year = year_m.group(1) if year_m else None
                    pub_date = f"{year}-01-01" if year else None

                    # Original filename: segment before @@download
                    fname_m = re.search(r"/([^/]+\.pdf)/", pdf_path, re.IGNORECASE)
                    original_filename = fname_m.group(1) if fname_m else None

                    # Extract abstract from PDF
                    print(f"[{self.site_id}] [{idx+1}/{len(pairs)}] extracting PDF text: {title}")
                    abstract = self._extract_pdf_text(pdf_url)

                    if not abstract or len(abstract) < 50:
                        print(f"[{self.site_id}] abstract <50 chars for {title!r}, skipping")
                        continue

                    detail_url = (
                        f"{self.base_url}/en/public/corporate-publications"
                        + (f"#{anchor}" if anchor else "")
                    )
                    post_number = year if year else anchor if anchor else str(idx)

                    paper = {
                        "site_id": self.site_id,
                        "external_id": anchor or pdf_path,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "published_date": pub_date,
                        "listed_date": pub_date,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "publisher": _PUBLISHER,
                        "category": "Activity Report",
                        "keywords": "synchrotron,ALBA,activity report,annual report",
                        "metadata": json.dumps({
                            "anchor": anchor,
                            "year": year,
                            "originalFilename": original_filename,
                            "posted_date": pub_date,
                        }),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}: {title}")

                    time.sleep(self._delay)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {idx} ({title!r}) failed: {exc}")
                    continue

        print(f"[{self.site_id}] page 1: saved {saved}/{limit_or_inf}")
        return saved
