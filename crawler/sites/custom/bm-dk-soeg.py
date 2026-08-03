# -*- coding: utf-8 -*-
"""bm.dk Søg crawler — Publikation content type.

Fetches the publication list from the Ankiro REST search API at
https://bm.ankiro.dk/Public/Rest/Search/bm.dk.json then visits each
detail page to extract abstract, PDF URL, and publish date.
"""

import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urlparse

# Absolute import: works both as a package module and when loaded via
# spec_from_file_location (which has no package context).
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402

_API_URL = "https://bm.ankiro.dk/Public/Rest/Search/bm.dk.json"
_BASE_STR = "Ym0uZGstNjM5Mg=="   # base64 key from jsViewData on bm.dk/soeg/
_PAGE_SIZE = 10
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_WALL_CLOCK_LIMIT = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))   # 25 minutes


class BmDkSoegCrawler(BaseCrawler):
    """Crawler for bm.dk Søg — Publikation (Danish Ministry of Employment)."""

    site_id = "bm-dk-soeg"
    site_name = "Custom: bm-dk-soeg"
    base_url = "https://bm.dk"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, retries: int = 3, accept: str = "*/*") -> str | None:
        """HTTP GET via curl; returns decoded text or None after retries."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", f"Accept: {accept}",
            "-H", "Accept-Language: da-DK,da;q=0.9,en;q=0.7",
            url,
        ]
        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                raw = result.stdout
                if raw and raw.strip():
                    try:
                        return raw.decode("utf-8")
                    except UnicodeDecodeError:
                        return raw.decode("utf-8", errors="replace")
                if attempt < retries - 1:
                    wait = 3 ** attempt   # 1s, 3s, 9s
                    print(f"[bm-dk-soeg] empty response for {url[:80]}, retry in {wait}s…")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < retries - 1:
                    wait = 3 ** attempt
                    print(f"[bm-dk-soeg] curl error ({exc}), retry in {wait}s…")
                    time.sleep(wait)
                else:
                    print(f"[bm-dk-soeg] curl failed after {retries} attempts: {exc}")
        return None

    def _fetch_api_page(self, start_index: int) -> dict | None:
        """Fetch one page from the Ankiro search API.

        Pagination: startIndex is 1-based; page N → startIndex=(N-1)*10+1.
        """
        url = (
            f"{_API_URL}?q=*&ContentTags=Publikation&sort-desc=ManualDate"
            f"&maxResults={_PAGE_SIZE}&startIndex={start_index}"
            f"&baseStr={_BASE_STR}"
        )
        raw = self._curl_get(url, accept="application/json")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[bm-dk-soeg] JSON decode error at startIndex={start_index}: {exc}")
            return None

    def _fetch_detail(self, url: str) -> tuple:
        """Fetch a publication detail page.

        Returns (abstract: str, pdf_url: str|None, published_date: str|None).
        """
        raw = self._curl_get(url, accept="text/html,*/*")
        if not raw:
            return "", None, None

        abstract = ""
        pdf_url = None
        published_date = None

        # --- BeautifulSoup parsing (preferred) ---
        soup = None
        try:
            from bs4 import BeautifulSoup
            for parser in ("html5lib", "lxml", "html.parser"):
                try:
                    soup = BeautifulSoup(raw, parser)
                    break
                except Exception:
                    continue
        except Exception:
            soup = None

        if soup:
            # Published date
            meta_pub = soup.find("meta", {"name": "publish-date"})
            if meta_pub:
                published_date = (meta_pub.get("content") or "").strip()[:10]

            # Abstract: article-lead (intro) + article-rte paragraphs (body)
            parts = []

            lead = soup.find(class_=re.compile(r"\barticle-lead\b"))
            if lead:
                txt = lead.get_text(separator=" ", strip=True)
                txt = re.sub(r"\s+", " ", txt).strip()
                if txt:
                    parts.append(txt)

            rte = soup.find(class_=re.compile(r"\barticle-rte\b"))
            if rte:
                for p in rte.find_all("p"):
                    txt = p.get_text(separator=" ", strip=True)
                    txt = re.sub(r"\s+", " ", txt).strip()
                    if txt and len(txt) > 30 and txt not in parts:
                        parts.append(txt)

            abstract = "\n\n".join(parts)

            # PDF link
            for a in soup.find_all("a", href=True):
                href = str(a["href"])
                if href.lower().endswith(".pdf"):
                    if not href.startswith("http"):
                        href = self.base_url + href
                    pdf_url = href
                    break

        # --- Regex fallback ---
        if not abstract:
            # Extract paragraphs from raw HTML
            para_hits = re.findall(r"<p[^>]*>(.*?)</p>", raw, re.DOTALL | re.IGNORECASE)
            chunks = []
            for p in para_hits:
                txt = re.sub(r"<[^>]+>", " ", p)
                txt = re.sub(r"&#\d+;", lambda m: chr(int(m.group(0)[2:-1])), txt)
                txt = re.sub(r"&[a-zA-Z]+;", " ", txt)
                txt = re.sub(r"\s+", " ", txt).strip()
                if len(txt) > 50:
                    chunks.append(txt)
            abstract = "\n\n".join(chunks)

        if not pdf_url:
            m = re.search(r'href="([^"]*\.pdf)"', raw, re.IGNORECASE)
            if m:
                href = m.group(1)
                if not href.startswith("http"):
                    href = self.base_url + href
                pdf_url = href

        if not published_date:
            m = re.search(r'name="publish-date"[^>]+content="([^"]+)"', raw, re.IGNORECASE)
            if not m:
                m = re.search(r'content="([^"]+)"[^>]+name="publish-date"', raw, re.IGNORECASE)
            if m:
                published_date = m.group(1).strip()[:10]

        return abstract, pdf_url, published_date

    @staticmethod
    def _parse_iso_date(raw: str) -> str:
        """Trim ISO datetime to YYYY-MM-DD. Returns '' on empty input."""
        return (raw or "")[:10]

    @staticmethod
    def _filename_from_url(url: str | None) -> str | None:
        """Extract the last path segment (filename) from a URL."""
        if not url:
            return None
        path = urlparse(url).path
        name = path.rstrip("/").split("/")[-1]
        return name if name else None

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl bm.dk Søg publikationer.

        Parameters
        ----------
        limit:
            Maximum number of documents to save. None means unlimited.
        """
        saved = 0
        start_time = time.time()
        seen_urls: set = set()
        page_num = 0
        total_results = None

        while True:
            # Wall-clock budget
            if time.time() - start_time > _WALL_CLOCK_LIMIT:
                print("[bm-dk-soeg] 25-minute wall-clock budget reached; exiting cleanly.")
                break

            # Limit reached
            if limit is not None and saved >= limit:
                break

            # Safety cap
            if page_num >= _MAX_PAGES:
                print(f"[bm-dk-soeg] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break

            # Progress log every 10 pages
            if page_num > 0 and page_num % 10 == 0:
                limit_str = str(limit) if limit is not None else "∞"
                print(f"[bm-dk-soeg] page {page_num}: saved {saved}/{limit_str}")

            # startIndex is 1-based; page 0 → startIndex=1, page 1 → startIndex=11, …
            start_index = page_num * _PAGE_SIZE + 1

            # Fetch API page with retries
            data = None
            for attempt in range(3):
                data = self._fetch_api_page(start_index)
                if data is not None:
                    break
                wait = 3 ** attempt
                print(f"[bm-dk-soeg] API fetch attempt {attempt + 1}/3 failed, "
                      f"retry in {wait}s…")
                time.sleep(wait)

            if data is None:
                print(f"[bm-dk-soeg] API page {page_num} failed after 3 retries. Stopping.")
                break

            if total_results is None:
                total_results = data.get("TotalResults", 0)
                print(f"[bm-dk-soeg] Total publications on server: {total_results}")

            docs = data.get("Documents", [])
            if not docs:
                print(f"[bm-dk-soeg] No docs on page {page_num}. Done.")
                break

            new_on_page = 0

            for doc in docs:
                if limit is not None and saved >= limit:
                    break

                doc_url = (doc.get("FriendlyUri") or doc.get("Uri") or "").strip()
                if not doc_url:
                    continue

                # URL-based dedup (prevents infinite loop if paginator loops back)
                if doc_url in seen_urls:
                    continue
                seen_urls.add(doc_url)
                new_on_page += 1

                doc_id = str(doc.get("Id", ""))
                props = {
                    p["Name"]: (p.get("Value") or "")
                    for p in doc.get("Properties", [])
                }

                title = (props.get("_shadowTitle") or props.get("Title") or "").strip()
                if not title:
                    print(f"[bm-dk-soeg] Skipping doc {doc_id}: no title")
                    continue

                manual_date = props.get("ManualDate", "")
                listed_date = self._parse_iso_date(manual_date)
                keywords = props.get("Keywords", "") or ""
                category = props.get("ContentTags", "") or ""
                section = props.get("Section", "") or ""
                subsection = props.get("Subsection", "") or ""

                # Per-item failure isolation
                try:
                    time.sleep(self._delay)
                    abstract, pdf_url, published_date = self._fetch_detail(doc_url)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[bm-dk-soeg] item {doc_id} failed: {exc}")
                    continue

                # Skip items with short abstracts
                if not abstract or len(abstract) < 50:
                    print(f"[bm-dk-soeg] Skipping '{title[:50]}': "
                          f"abstract too short ({len(abstract)} chars)")
                    continue

                if not published_date:
                    published_date = listed_date

                original_filename = self._filename_from_url(pdf_url)

                paper = {
                    "site_id": self.site_id,
                    "external_id": doc_id,
                    "post_number": doc_id,
                    "title": title,
                    "abstract": abstract,
                    "published_date": published_date,
                    "listed_date": listed_date,
                    "url": doc_url,
                    "pdf_url": pdf_url or "",
                    "keywords": keywords,
                    "category": category,
                    "department": subsection or section,
                    "publisher": "Beskæftigelsesministeriet",
                    "authors": "",
                    "journal": "",
                    "doi": "",
                    "original_filename": original_filename,
                    "metadata": json.dumps({
                        "posted_date": manual_date,
                        "originalFilename": original_filename,
                        "section": section,
                        "subsection": subsection,
                        "lastUpdated": doc.get("LastUpdated", ""),
                        "created": doc.get("Created", ""),
                        "dataSourceId": doc.get("DataSourceId"),
                        "ankiroId": doc_id,
                    }, ensure_ascii=False),
                }

                try:
                    self._save_paper(paper)
                    saved += 1
                    print(f"[bm-dk-soeg] Saved {saved}: {title[:60]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[bm-dk-soeg] item {doc_id} save failed: {exc}")
                    continue

            # End-of-pagination checks
            if new_on_page == 0:
                print(f"[bm-dk-soeg] Page {page_num}: 0 new docs (all seen). Done.")
                break

            # Stop when we've consumed all available records
            if total_results is not None:
                fetched_up_to = start_index - 1 + len(docs)
                if fetched_up_to >= total_results:
                    print(f"[bm-dk-soeg] Reached end of results ({total_results} total). Done.")
                    break

            page_num += 1

        print(f"[bm-dk-soeg] Done. Total saved: {saved}")
        return saved
