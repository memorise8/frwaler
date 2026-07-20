# -*- coding: utf-8 -*-
"""INERIS base documentaire crawler — Communiqués de presse (document_type:68).

Listing: https://www.ineris.fr/fr/base-documentaire?document[0]=document_type:68
Pagination: &page=N (9 items/page, ~153 total as of 2026-05).
Each card has: title, date (DD.MM.YYYY), category, direct PDF URL.
No HTML detail page — abstract is extracted from PDF via pdftotext.
"""

import json
import os
import re
import subprocess
import tempfile
import time
from urllib.parse import unquote

from crawler.base_crawler import BaseCrawler

_BASE = "https://www.ineris.fr"
_LIST_PATH = "/fr/base-documentaire"
_LIST_FILTER = "?document%5B0%5D=document_type%3A68"
_PAGE_SIZE = 9          # observed items per page on the listing
_MAX_PAGES = 200        # safety cap
_MAX_WALL = 25 * 60    # 25-minute wall-clock budget


class InerisfrfrCrawler(BaseCrawler):
    site_id = "ineris-fr-fr"
    site_name = "Custom: ineris-fr-fr"
    base_url = "https://www.ineris.fr"

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, retries=3, accept="text/html"):
        """GET via curl with retry/backoff. Returns decoded text or None."""
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    [
                        "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
                        "-H", f"User-Agent: {self.USER_AGENT}",
                        "-H", f"Accept: {accept}",
                        url,
                    ],
                    capture_output=True,
                    timeout=35,
                )
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                wait = (attempt + 1) ** 2
                if attempt < retries - 1:
                    print(f"[{self.site_id}] Empty response for {url[:60]}, retry in {wait}s")
                    time.sleep(wait)
            except Exception as exc:
                wait = (attempt + 1) ** 2
                if attempt < retries - 1:
                    print(f"[{self.site_id}] curl error ({exc}), retry in {wait}s")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after {retries} attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # Listing page parser
    # ------------------------------------------------------------------

    def _parse_listing(self, html):
        """Parse a listing page HTML. Returns list of dicts (title/date_raw/category/pdf_url)."""
        items = []
        try:
            soup = self._make_soup(html)
            for fig in soup.find_all("figure"):
                cap = fig.find("figcaption")
                if not cap:
                    continue

                title_el = cap.find("h3", class_="ttl")
                title = title_el.get_text(strip=True) if title_el else ""
                if not title:
                    continue

                category, date_raw = "", ""
                meta = cap.find("p", class_="metadata-wrapper")
                if meta:
                    ft = meta.find("i", class_="metadata-file-type")
                    if ft:
                        category = re.sub(r"\s*\.pdf\s*$", "", ft.get_text(strip=True), flags=re.I).strip()
                    te = meta.find("i", class_="metadata-time")
                    if te:
                        date_raw = te.get_text(strip=True)

                pdf_url = ""
                btn = cap.find("a", class_="btn")
                if btn and btn.get("href"):
                    href = btn["href"]
                    pdf_url = href if href.startswith("http") else _BASE + href

                items.append({
                    "title": title,
                    "date_raw": date_raw,
                    "category": category,
                    "pdf_url": pdf_url,
                })
        except Exception as exc:
            print(f"[{self.site_id}] Listing parse error: {exc}")
        return items

    def _make_soup(self, html):
        """BeautifulSoup with html5lib → lxml → html.parser fallback chain."""
        from bs4 import BeautifulSoup
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return BeautifulSoup(html, "html.parser")

    # ------------------------------------------------------------------
    # PDF text extraction
    # ------------------------------------------------------------------

    def _extract_pdf_text(self, pdf_url, max_chars=3000):
        """Download PDF and extract text with pdftotext. Returns text or None."""
        if not pdf_url:
            return None

        tmp_path = None
        for attempt in range(3):
            try:
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    tmp_path = tmp.name

                dl = subprocess.run(
                    [
                        "curl", "-skL", "--tls-max", "1.3", "--max-time", "45",
                        "-H", f"User-Agent: {self.USER_AGENT}",
                        "-o", tmp_path, pdf_url,
                    ],
                    capture_output=True,
                    timeout=50,
                )

                if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) < 200:
                    if attempt < 2:
                        time.sleep((attempt + 1) * 3)
                        continue
                    return None

                txt = subprocess.run(
                    ["pdftotext", "-l", "4", tmp_path, "-"],
                    capture_output=True, text=True, timeout=30,
                )
                text = txt.stdout.strip()
                if text:
                    text = re.sub(r"\n{3,}", "\n\n", text)
                    text = re.sub(r"[ \t]+", " ", text)
                    return text[:max_chars]

                if attempt < 2:
                    time.sleep((attempt + 1) * 3)

            except Exception as exc:
                if attempt < 2:
                    time.sleep((attempt + 1) * 3)
                else:
                    print(f"[{self.site_id}] PDF extract failed ({pdf_url[:60]}): {exc}")
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass
                tmp_path = None

        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_date(date_raw):
        """Convert DD.MM.YYYY to YYYY-MM-DD. Returns empty string on failure."""
        if not date_raw:
            return ""
        m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", date_raw.strip())
        if m:
            return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
        return date_raw

    @staticmethod
    def _external_id(pdf_url):
        """Stable external_id from PDF URL filename (without extension)."""
        if not pdf_url:
            return None
        path = pdf_url.split("?")[0]
        filename = path.rsplit("/", 1)[-1]
        decoded = unquote(filename)
        base = re.sub(r"\.pdf$", "", decoded, flags=re.I)
        return base[:200] if base else None

    @staticmethod
    def _post_number(ext_id):
        """Prefer the trailing numeric timestamp in the filename as post_number."""
        if not ext_id:
            return None
        m = re.search(r"(\d{7,})$", ext_id)
        return m.group(1) if m else ext_id

    @staticmethod
    def _original_filename(pdf_url):
        """Extract original filename from URL last path segment."""
        if not pdf_url:
            return None
        path = pdf_url.split("?")[0]
        return unquote(path.rsplit("/", 1)[-1])

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl INERIS communiqués de presse (document_type:68).

        Paginates the listing (?page=N), downloads each PDF to extract an
        abstract via pdftotext, then saves via _save_paper().
        """
        saved = 0
        seen_urls = set()
        start_time = time.time()

        for page_num in range(_MAX_PAGES):
            if page_num == _MAX_PAGES - 1:
                print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached, stopping.")

            if time.time() - start_time > _MAX_WALL:
                print(f"[{self.site_id}] Wall-clock budget ({_MAX_WALL}s) reached, stopping.")
                break

            if limit is not None and saved >= limit:
                break

            list_url = f"{_BASE}{_LIST_PATH}{_LIST_FILTER}&page={page_num}"

            html = None
            for attempt in range(3):
                html = self._curl_get(list_url)
                if html and "<figure" in html:
                    break
                wait = (attempt + 1) ** 2
                print(f"[{self.site_id}] Page {page_num} fetch failed (attempt {attempt+1}/3), "
                      f"retry in {wait}s")
                time.sleep(wait)

            if not html or "<figure" not in html:
                print(f"[{self.site_id}] Page {page_num}: no figures returned. Stopping.")
                break

            items = self._parse_listing(html)
            if not items:
                print(f"[{self.site_id}] Page {page_num}: no items parsed. Stopping.")
                break

            new_items = [it for it in items if it.get("pdf_url") and it["pdf_url"] not in seen_urls]
            if not new_items:
                print(f"[{self.site_id}] Page {page_num}: all items already seen. Stopping.")
                break

            if page_num > 0 and page_num % 10 == 0:
                lim_str = str(limit) if limit is not None else "∞"
                print(f"[{self.site_id}] page {page_num}: saved {saved}/{lim_str}")

            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                pdf_url = item["pdf_url"]
                if pdf_url in seen_urls:
                    continue
                seen_urls.add(pdf_url)

                try:
                    ext_id = self._external_id(pdf_url)
                    if not ext_id:
                        print(f"[{self.site_id}] Cannot derive external_id for {pdf_url[:60]}, skipping")
                        continue

                    title = item["title"]
                    category = item["category"]
                    date_str = self._parse_date(item["date_raw"])
                    orig_fn = self._original_filename(pdf_url)
                    post_num = self._post_number(ext_id)

                    time.sleep(self._delay)
                    abstract = self._extract_pdf_text(pdf_url)

                    if not abstract or len(abstract) < 100:
                        print(f"[{self.site_id}] Abstract too short (<100) for '{title[:40]}', skipping")
                        continue

                    paper = {
                        "site_id": self.site_id,
                        "external_id": ext_id,
                        "post_number": post_num,
                        "title": title,
                        "abstract": abstract,
                        "published_date": date_str,
                        "posted_date": date_str,
                        "url": pdf_url,
                        "pdf_url": pdf_url,
                        "category": category,
                        "publisher": "INERIS",
                        "authors": "",
                        "keywords": "",
                        "original_filename": orig_fn,
                        "metadata": json.dumps(
                            {
                                "posted_date": item["date_raw"],
                                "originalFilename": orig_fn,
                                "document_type": "68",
                                "category_raw": category,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    lim_str = str(limit) if limit is not None else "∞"
                    print(f"[{self.site_id}] Saved {saved}/{lim_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] Item failed: {exc}")
                    continue

            time.sleep(0.5)

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
