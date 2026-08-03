# -*- coding: utf-8 -*-
"""Crawler for SiS (Statens institutionsstyrelse) remissvar.

Source: https://www.stat-inst.se/om-sis/remissvar/
All items are on a single HTML page with direct links to PDFs.
Abstract text is extracted from each PDF using pdftotext.
"""

from __future__ import annotations

import html as html_lib
import json
import os
import re
import subprocess
import tempfile
import time

from crawler.base_crawler import BaseCrawler

_SITE_ID = "stat-inst-se-om-sis"
_BASE_URL = "https://www.stat-inst.se"
_LIST_URL = "https://www.stat-inst.se/om-sis/remissvar/"
_ABSTRACT_MIN_CHARS = 50
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))  # safety cap (this site is single-page, but kept for robustness)
_MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))


class StatInstSisCrawler(BaseCrawler):
    """Crawler for SiS (Statens institutionsstyrelse) consultation responses."""

    site_id = _SITE_ID
    site_name = "Custom: stat-inst-se-om-sis"
    base_url = _BASE_URL

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, binary: bool = False, timeout: int = 60):
        """GET via curl with exponential-backoff retry (1s, 3s, 9s).

        Returns bytes when binary=True, str otherwise. Returns None on failure.
        """
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "-L",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
                if result.returncode == 0 and result.stdout:
                    return result.stdout if binary else result.stdout.decode("utf-8", errors="replace")
            except subprocess.TimeoutExpired:
                pass
            except Exception as exc:
                print(f"[{_SITE_ID}] curl error for {url!r}: {exc}")
            wait = 3 ** attempt  # 1, 3, 9 seconds
            if attempt < 2:
                print(f"[{_SITE_ID}] Retry {attempt+1}/3 in {wait}s for {url!r}")
                time.sleep(wait)
        print(f"[{_SITE_ID}] Failed after 3 attempts: {url!r}")
        return None

    # ------------------------------------------------------------------
    # PDF extraction
    # ------------------------------------------------------------------

    def _extract_pdf_text(self, pdf_url: str) -> str | None:
        """Download PDF and extract text with pdftotext. Returns text or None."""
        pdf_bytes = self._curl_get(pdf_url, binary=True, timeout=60)
        if not pdf_bytes or len(pdf_bytes) < 100:
            return None

        tf = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tmppath = tf.name
        try:
            tf.write(pdf_bytes)
            tf.flush()
            tf.close()
            result = subprocess.run(
                ["pdftotext", tmppath, "-"],
                capture_output=True,
                timeout=30,
            )
            text = result.stdout.decode("utf-8", errors="replace").strip()
            return text if text else None
        except Exception as exc:
            print(f"[{_SITE_ID}] pdftotext error for {pdf_url}: {exc}")
            return None
        finally:
            try:
                os.unlink(tmppath)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_date(text: str) -> str | None:
        """Extract first ISO-ish date (YYYY-MM-DD) from text."""
        if not text:
            return None
        m = re.search(r'\b(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})\b', text[:600])
        if m:
            return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
        return None

    @staticmethod
    def _parse_dnr(text: str) -> str | None:
        """Extract Dnr reference number from PDF text."""
        if not text:
            return None
        m = re.search(r'Dnr\s+([\d.\-/]+)', text[:600])
        return m.group(1).strip() if m else None

    def _parse_listing(self, html: str) -> list[tuple[str, str, str]]:
        """Parse (title, pdf_url, year) tuples from the listing page.

        Tries html5lib → lxml → html.parser; falls back to regex.
        """
        soup = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, parser)
                break
            except Exception:
                continue

        items: list[tuple[str, str, str]] = []

        if soup:
            main = soup.find("main") or soup
            current_year = ""
            for el in main.find_all(["h2", "li"]):
                if el.name == "h2":
                    txt = el.get_text(strip=True)
                    if re.match(r'^\d{4}$', txt):
                        current_year = txt
                elif el.name == "li":
                    if "list__item" not in (el.get("class") or []):
                        continue
                    a = el.find("a", href=True)
                    if not a:
                        continue
                    href = a.get("href", "")
                    if not href.lower().endswith(".pdf"):
                        continue
                    full_url = (_BASE_URL + href) if href.startswith("/") else href
                    raw = a.get_text(separator=" ", strip=True)
                    raw = re.sub(r'\(\s*,?\s*nytt\s+f[öo]nster\s*\)', '', raw, flags=re.I)
                    title = re.sub(r'\s+', ' ', raw).strip()
                    if title and full_url:
                        items.append((title, full_url, current_year))
        else:
            # Regex fallback
            content = html
            start = html.find('<main')
            if start != -1:
                content = html[start:]
            current_year = ""
            pat = re.compile(
                r'<h2[^>]*>\s*(\d{4})\s*</h2>|<li\s+class="list__item">(.*?)</li>',
                re.DOTALL,
            )
            for m in pat.finditer(content):
                if m.group(1):
                    current_year = m.group(1)
                else:
                    li = m.group(2)
                    href_m = re.search(r'href="([^"]+\.pdf)"', li, re.I)
                    title_m = re.search(r'<a[^>]+>(.*?)\(', li, re.DOTALL)
                    if href_m and title_m:
                        href = href_m.group(1)
                        full_url = (_BASE_URL + href) if href.startswith("/") else href
                        raw = re.sub(r'<[^>]+>', '', title_m.group(1))
                        title = html_lib.unescape(re.sub(r'\s+', ' ', raw).strip())
                        if title and full_url:
                            items.append((title, full_url, current_year))

        return items

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        saved = 0
        seen_urls: set[str] = set()
        limit_str = str(limit) if limit is not None else "inf"

        # Fetch the single listing page (all items are on one page)
        print(f"[{_SITE_ID}] Fetching listing: {_LIST_URL}")
        html = self._curl_get(_LIST_URL)
        if not html:
            print(f"[{_SITE_ID}] Failed to fetch listing page.")
            return 0

        items = self._parse_listing(html)
        total = len(items)
        print(f"[{_SITE_ID}] Found {total} items on listing page.")

        if not items:
            print(f"[{_SITE_ID}] No items parsed. Done.")
            return 0

        # This site has a single listing page; the loop runs once (p=1).
        # The _MAX_PAGES cap is kept for structural robustness.
        page = 1
        idx = 0

        while idx < total:
            if page > _MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if time.time() - start_time > _MAX_SECONDS:
                print(f"[{_SITE_ID}] 25-minute budget reached. Stopping.")
                break

            title, pdf_url, year = items[idx]
            idx += 1

            if pdf_url in seen_urls:
                continue
            seen_urls.add(pdf_url)

            try:
                time.sleep(self._delay)

                title = html_lib.unescape(title)
                filename = pdf_url.rstrip("/").split("/")[-1]
                external_id = re.sub(r'\.pdf$', '', filename, flags=re.I)

                print(f"[{_SITE_ID}] [{idx}/{total}] {filename[:70]}")
                pdf_text = self._extract_pdf_text(pdf_url)

                if not pdf_text or len(pdf_text.strip()) < _ABSTRACT_MIN_CHARS:
                    print(f"[{_SITE_ID}] Skipping (abstract too short): {title[:60]}")
                    continue

                abstract = pdf_text[:3000].strip()
                published_date = self._parse_date(pdf_text)
                if not published_date and year:
                    published_date = f"{year}-01-01"

                dnr = self._parse_dnr(pdf_text)

                paper = {
                    "id": None,
                    "site_id": _SITE_ID,
                    "external_id": external_id,
                    "post_number": external_id,
                    "title": title,
                    "abstract": abstract,
                    "published_date": published_date,
                    "posted_date": f"{year}-01-01" if year else None,
                    "authors": None,
                    "publisher": "Statens institutionsstyrelse",
                    "department": None,
                    "journal": None,
                    "url": _LIST_URL,
                    "pdf_url": pdf_url,
                    "doi": None,
                    "keywords": None,
                    "category": "Remissvar",
                    "original_filename": filename,
                    "metadata": json.dumps({
                        "dnr": dnr,
                        "year": year,
                        "posted_date": f"{year}-01-01" if year else None,
                        "originalFilename": filename,
                    }, ensure_ascii=False),
                }

                self._save_paper(paper)
                saved += 1
                print(f"[{_SITE_ID}] Saved {saved}/{limit_str}: {title[:60]}")

                if idx % 10 == 0:
                    print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_str}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] item {idx} failed: {exc}")
                continue

            # Single-page site: after processing all items on this page, stop.
            if idx >= total:
                break

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
