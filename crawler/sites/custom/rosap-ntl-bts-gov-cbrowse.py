# -*- coding: utf-8 -*-
"""Crawler for ROSAP (National Transportation Library) Tech Report records.

Browse endpoint: https://rosap.ntl.bts.gov/cbrowse
  ?pid=dot%3A231&parentId=dot%3A231&sm_resource_type%5B%5D=Tech+Report
  Paginates via &start=N (20 items/page; ~3960 total as of 2026-05).

Detail page: https://rosap.ntl.bts.gov/view/dot/{id}
  Rich citation_* meta tags supply all metadata fields.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class RosapNtlBtsGovCbrowseCrawler(BaseCrawler):
    site_id = "rosap-ntl-bts-gov-cbrowse"
    site_name = "Custom: rosap-ntl-bts-gov-cbrowse"
    base_url = "https://rosap.ntl.bts.gov"

    _PAGE_SIZE = 20
    _MAX_PAGES = 200
    _MAX_WALL_SECONDS = 25 * 60  # 25 minutes

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl(self, url, *, timeout=60):
        # IMPORTANT: rosap.ntl.bts.gov (Akamai CDN) blocks Chrome UA strings but
        # allows the default curl UA. Do NOT set User-Agent or extra headers here.
        cmd = [
            "curl", "-sk",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            url,
        ]
        waits = [1, 3, 9]
        last_error = "unknown"
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 10, check=False
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                if result.returncode == 0 and result.stdout:
                    return result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} {stderr[:120]}"

            if attempt < 2:
                wait = waits[attempt]
                print(
                    f"[{self.site_id}] curl failed {attempt+1}/3 for {url}: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_html(self, raw):
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _clean(text):
        if not text:
            return ""
        text = unescape(str(text))
        text = text.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
        return re.sub(r"[ \t]+", " ", text).strip()

    # ------------------------------------------------------------------
    # Browse page
    # ------------------------------------------------------------------

    def _browse_url(self, start):
        return (
            f"https://rosap.ntl.bts.gov/cbrowse"
            f"?pid=dot%3A231&parentId=dot%3A231"
            f"&sm_resource_type%5B%5D=Tech+Report"
            f"&start={start}"
        )

    def _extract_browse_items(self, html):
        """Return ordered list of numeric item IDs from browse page HTML."""
        if not html:
            return []
        soup = self._parse_html(html)
        if soup is None:
            # fallback: regex
            return re.findall(r'href=["\'](?:/view/dot/(\d+))["\']', html)
        seen = set()
        ids = []
        for tag in soup.find_all("a", href=True):
            m = re.match(r"^/view/dot/(\d+)$", tag["href"])
            if m:
                num = m.group(1)
                if num not in seen:
                    seen.add(num)
                    ids.append(num)
        return ids

    def _extract_total(self, html):
        """Return total record count from 'Showing X-Y of N' banner."""
        if not html:
            return None
        m = re.search(r"Showing\s+\d[\d,]*\s*-\s*\d[\d,]*\s+of\s+([\d,]+)", html, re.IGNORECASE)
        if m:
            return int(m.group(1).replace(",", ""))
        return None

    # ------------------------------------------------------------------
    # Detail page
    # ------------------------------------------------------------------

    def _fetch_detail(self, item_id):
        """Fetch detail page and return a paper dict, or None on failure."""
        url = f"{self.base_url}/view/dot/{item_id}"
        html = self._curl(url, timeout=45)
        if not html:
            return None

        soup = self._parse_html(html)
        if soup is None:
            return None

        def meta_first(name):
            tag = soup.find("meta", attrs={"name": name})
            return self._clean(tag.get("content", "") if tag else "")

        def meta_all(name):
            return [
                self._clean(t.get("content", ""))
                for t in soup.find_all("meta", attrs={"name": name})
                if t.get("content", "").strip()
            ]

        # Title
        title = meta_first("citation_title")
        if not title:
            h1 = soup.find("h1")
            title = self._clean(h1.get_text()) if h1 else f"dot:{item_id}"

        # Abstract — try citation_abstract first, fall back to description
        abstract = meta_first("citation_abstract")
        if not abstract or len(abstract) < 50:
            desc = meta_first("description")
            if len(desc) > len(abstract):
                abstract = desc

        # Keywords
        kw_list = meta_all("citation_keywords")
        keywords = ", ".join(k for k in kw_list if k)

        # Published date: "2025/01/01" → "2025-01-01"
        pub_date_raw = meta_first("citation_publication_date")
        published_date = pub_date_raw.replace("/", "-") if pub_date_raw else ""

        # PDF URL
        pdf_url = meta_first("citation_pdf_url")
        if pdf_url and not pdf_url.startswith("http"):
            pdf_url = urljoin(self.base_url, pdf_url)
        pdf_url = pdf_url or None

        # Authors (multiple citation_author tags)
        authors_list = meta_all("citation_author")
        authors = "; ".join(a for a in authors_list if a) or None

        # Publisher — look for "Corporate Publisher:" label in page
        publisher = None
        pub_label = soup.find(string=re.compile(r"Corporate Publisher", re.IGNORECASE))
        if pub_label:
            container = pub_label.find_parent()
            if container:
                anchor = container.find_next("a")
                if anchor:
                    publisher = self._clean(anchor.get_text())
        if not publisher:
            publisher = meta_first("citation_publisher") or None

        # DOI
        doi = meta_first("citation_doi") or None

        # Original filename from PDF URL path
        original_filename = None
        if pdf_url:
            tail = urlparse(pdf_url).path.split("/")[-1]
            if tail and "." in tail:
                original_filename = tail

        metadata = {
            "item_id": item_id,
            "fulltext_html_url": meta_first("citation_fulltext_html_url"),
            "language": meta_first("citation_language"),
        }
        if doi:
            metadata["doi"] = doi
        if kw_list:
            metadata["keywords_raw"] = kw_list

        return {
            "site_id": self.site_id,
            "external_id": item_id,           # numeric string e.g. "79090"
            "post_number": item_id,           # same — numeric, good for MAX() tracking
            "title": title,
            "abstract": abstract,
            "published_date": published_date or None,
            "listed_date": None,
            "authors": authors,
            "publisher": publisher,
            "url": url,                       # meta_url via adapter
            "pdf_url": pdf_url,
            "keywords": keywords or None,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_ids = set()
        start = 0
        total = None
        page_num = 0
        start_time = time.time()

        while True:
            # Wall-clock budget
            if time.time() - start_time > self._MAX_WALL_SECONDS:
                print(f"[{self.site_id}] Wall-clock budget (25 min) exceeded; stopping cleanly")
                break

            if limit is not None and saved >= limit:
                break

            if page_num >= self._MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached; stopping")
                break

            html = self._curl(self._browse_url(start))
            if not html:
                print(f"[{self.site_id}] Failed to fetch browse page start={start}; stopping")
                break

            if total is None:
                total = self._extract_total(html)
                if total:
                    print(f"[{self.site_id}] Total records: {total:,}")

            item_ids = self._extract_browse_items(html)
            if not item_ids:
                print(f"[{self.site_id}] No items at start={start}; end of listing")
                break

            new_ids = [i for i in item_ids if i not in seen_ids]
            if not new_ids:
                print(f"[{self.site_id}] All {len(item_ids)} items on page already seen; stopping (loop guard)")
                break

            for item_id in new_ids:
                seen_ids.add(item_id)

                if limit is not None and saved >= limit:
                    break

                try:
                    paper = self._fetch_detail(item_id)
                    if paper is None:
                        print(f"[{self.site_id}] item {item_id} failed: empty response")
                        continue

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {item_id} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    lim_str = str(limit) if limit is not None else "∞"
                    print(f"[{self.site_id}] saved {saved}/{lim_str}: {paper['title'][:80]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_id} failed: {exc}")
                    continue
                finally:
                    time.sleep(self._delay)

            page_num += 1
            if page_num % 10 == 0:
                lim_str = str(limit) if limit is not None else "∞"
                print(f"[{self.site_id}] page {page_num}: saved {saved}/{lim_str}")

            start += self._PAGE_SIZE

            if total is not None and start >= total:
                print(f"[{self.site_id}] Reached end of records (start={start} >= total={total})")
                break

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
