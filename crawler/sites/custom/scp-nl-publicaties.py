# -*- coding: utf-8 -*-
"""Crawler for SCP.nl publications (Sociaal en Cultureel Planbureau).

Starting URL: https://www.scp.nl/publicaties  (redirects → /documenten)

Discovery: The site is a Next.js app.  Publications are served by a REST
search endpoint (POST /api/search) that backs an Elastic App Search index.
Each result page yields list-level metadata; the detail page is fetched to
obtain the full abstract, PDF URL, authors, and publication date.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

# --------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------
_PUBLICATION_TYPES = [
    "Publicatie",
    "Rapport",
    "Kennisnotitie",
    "Factsheet",
    "Essay",
    "Beleidsnota",
    "Magazine",
    "Richtlijn",
    "Jaarplan",
]
_PAGE_SIZE = 20
_MIN_ABSTRACT_CHARS = 50
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_CRAWL_TIMEOUT_S = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes


class ScpNlPublicatiesCrawler(BaseCrawler):
    site_id = "scp-nl-publicaties"
    site_name = "Custom: scp-nl-publicaties"
    base_url = "https://www.scp.nl"

    _SEARCH_URL = "https://www.scp.nl/api/search"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url, *, timeout=45, post_json=None):
        """Fetch URL with curl (retry x3 with backoff). Returns text or None."""
        cmd = [
            "curl",
            "--tls-max", "1.3",
            "-skL",
            "--compressed",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/json,*/*;q=0.8",
            "-H", "Accept-Language: nl-NL,nl;q=0.9,en;q=0.8",
        ]
        if post_json is not None:
            cmd += [
                "-X", "POST",
                "-H", "Content-Type: application/json",
                "-d", json.dumps(post_json),
            ]
        cmd.append(url)

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
                stdout = result.stdout or b""
                if result.returncode == 0 and stdout.strip():
                    return stdout.decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} stderr={stderr[:200]}"
            if attempt < 2:
                wait = waits[attempt]
                print(
                    f"[{self.site_id}] curl {attempt + 1}/3 failed for {url}: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    @staticmethod
    def _parse_html(raw):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[scp-nl-publicaties] BeautifulSoup({parser}) failed: {exc}")
        return None

    @staticmethod
    def _clean(text):
        if not text:
            return ""
        text = str(text).replace("\xa0", " ").replace("\r\n", "\n")
        text = re.sub(r"[ \t]+", " ", text)
        return text.strip()

    # ------------------------------------------------------------------
    # Search API
    # ------------------------------------------------------------------

    def _search_page(self, page):
        """POST to /api/search; returns (results_list, total_pages)."""
        body = {
            "requestState": {
                "current": page,
                "filters": [
                    {
                        "field": "information_type",
                        "values": _PUBLICATION_TYPES,
                        "type": "any",
                    }
                ],
                "resultsPerPage": _PAGE_SIZE,
                "searchTerm": "",
                "sortList": [{"field": "sort_date", "direction": "desc"}],
            },
            "queryConfig": {
                "filters": [],
                "result_fields": {
                    "author": {"raw": {}},
                    "url": {"raw": {}},
                    "page_title": {"raw": {}},
                    "meta_description": {
                        "raw": {},
                        "snippet": {"size": 500, "fallback": True},
                    },
                    "sort_date": {"raw": {}},
                    "information_type": {"raw": {}},
                },
                "disjunctiveFacets": ["information_type"],
                "facets": {"information_type": {"type": "value", "size": 100}},
            },
        }

        raw = self._curl(self._SEARCH_URL, post_json=body)
        if not raw:
            return [], 0

        try:
            data = json.loads(raw)
        except Exception as exc:
            print(f"[{self.site_id}] JSON parse error on search page {page}: {exc}")
            return [], 0

        if not data.get("ok", True):
            msg = data.get("message", "unknown")
            print(f"[{self.site_id}] API error on page {page}: {msg}")
            return [], 0

        results = data.get("results", [])
        total_pages = data.get("totalPages", 1)
        return results, int(total_pages)

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, raw, _list_item):
        """Parse a detail page HTML.  Returns a dict with content fields."""
        soup = self._parse_html(raw)
        if soup is None:
            raise ValueError("detail page could not be parsed")

        # Abstract: div.intro div.rich-text > all <p> tags
        abstract = ""
        intro = soup.select_one("div.intro div.rich-text")
        if intro:
            paras = intro.find_all("p")
            abstract = " ".join(
                self._clean(p.get_text(" ", strip=True)) for p in paras
            ).strip()
        if not abstract:
            # Fallback: any rich-text block
            rt = soup.select_one("div.rich-text")
            if rt:
                abstract = self._clean(rt.get_text(" ", strip=True))
        if not abstract:
            # Final fallback: <meta name="description">
            m = soup.find("meta", attrs={"name": "description"})
            if m:
                abstract = self._clean(m.get("content", ""))

        # Published date: DCTERMS.issued (ISO datetime → YYYY-MM-DD)
        published_date = ""
        m = soup.find("meta", attrs={"name": "DCTERMS.issued"})
        if m:
            match = re.search(r"(\d{4}-\d{2}-\d{2})", m.get("content", ""))
            if match:
                published_date = match.group(1)

        # Author: DCTERMS.creator
        authors = ""
        m = soup.find("meta", attrs={"name": "DCTERMS.creator"})
        if m:
            authors = self._clean(m.get("content", ""))

        # Publisher: DCTERMS.publisher (default to SCP)
        publisher = "Sociaal en Cultureel Planbureau"
        m = soup.find("meta", attrs={"name": "DCTERMS.publisher"})
        if m:
            val = self._clean(m.get("content", ""))
            if val:
                publisher = val

        # PDF URL: first download-list link pointing at a .pdf file
        pdf_url = ""
        for a in soup.select("a.download-list__link[href]"):
            href = (a.get("href") or "").strip()
            if href.lower().endswith(".pdf"):
                pdf_url = href
                break
        if not pdf_url:
            # Fallback: any download-list link
            a = soup.select_one("a.download-list__link[href]")
            if a:
                pdf_url = (a.get("href") or "").strip()

        # original_filename from the PDF URL path
        original_filename = ""
        if pdf_url:
            original_filename = urlparse(pdf_url).path.rstrip("/").split("/")[-1]

        # elastic-content script tag — additional metadata
        elastic = {}
        ec = soup.find("script", attrs={"id": "elastic-content"})
        if ec and ec.string:
            try:
                elastic = json.loads(ec.string)
            except Exception:
                pass

        return {
            "abstract": abstract,
            "published_date": published_date,
            "authors": authors,
            "publisher": publisher,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "elastic": elastic,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        start_time = time.monotonic()
        limit_str = str(limit) if limit is not None else "∞"

        page = 1
        total_pages = None

        while True:
            # 25-minute wall-clock budget
            if time.monotonic() - start_time > _CRAWL_TIMEOUT_S:
                print(f"[{self.site_id}] 25-minute timeout; stopping at {saved} saved")
                break

            if limit is not None and saved >= limit:
                break

            if page > _MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached; stopping")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            results, tp = self._search_page(page)

            if total_pages is None:
                total_pages = tp
                print(f"[{self.site_id}] Total pages: {total_pages}, limit: {limit_str}")

            if not results:
                print(f"[{self.site_id}] No results on page {page}; stopping")
                break

            for idx, item in enumerate(results, start=1):
                if limit is not None and saved >= limit:
                    break

                try:
                    # ---- list-level fields ----
                    raw_url = ((item.get("url") or {}).get("raw") or "").strip()
                    if not raw_url:
                        print(f"[{self.site_id}] p{page} item {idx}: no URL, skipping")
                        continue

                    full_url = urljoin(self.base_url, raw_url)

                    if full_url in seen_urls:
                        continue
                    seen_urls.add(full_url)

                    title = self._clean(
                        (item.get("page_title") or {}).get("raw", "")
                    )
                    if not title:
                        print(f"[{self.site_id}] p{page} item {idx}: no title, skipping")
                        continue

                    # external_id: Elastic doc ID e.g. "doc-6a1846706eab3398fc3b9438"
                    external_id = (
                        (item.get("id") or {}).get("raw", "")
                        or (item.get("_meta") or {}).get("id", "")
                    ).strip()
                    if not external_id:
                        print(f"[{self.site_id}] p{page} item {idx}: no external_id, skipping")
                        continue

                    sort_date_raw = (
                        (item.get("sort_date") or {}).get("raw") or ""
                    ).strip()
                    listed_date = ""
                    post_number = None
                    if sort_date_raw:
                        m = re.search(r"(\d{4}-\d{2}-\d{2})", sort_date_raw)
                        if m:
                            listed_date = m.group(1)
                        # Use epoch seconds as numeric post_number for ordering
                        try:
                            from datetime import datetime, timezone
                            dt = datetime.fromisoformat(
                                sort_date_raw.replace("Z", "+00:00")
                            )
                            post_number = str(int(dt.timestamp()))
                        except Exception:
                            post_number = listed_date.replace("-", "") if listed_date else None

                    category = self._clean(
                        (item.get("information_type") or {}).get("raw", "")
                    )

                    # ---- detail page ----
                    if self.detail_delay:
                        time.sleep(self.detail_delay)

                    detail_raw = self._curl(full_url)
                    if not detail_raw:
                        print(
                            f"[{self.site_id}] p{page} item {idx}: "
                            f"detail fetch failed for {full_url}"
                        )
                        continue

                    detail = self._parse_detail(detail_raw, item)
                    abstract = detail["abstract"]

                    if len(abstract.strip()) < _MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] p{page} item {idx}: abstract too short "
                            f"({len(abstract.strip())} chars) for {external_id}; skipping"
                        )
                        continue

                    elastic = detail.get("elastic") or {}
                    metadata = {
                        "doc_id": external_id,
                        "sort_date": sort_date_raw,
                        "posted_date": sort_date_raw,
                        "elastic_content": elastic,
                    }

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "published_date": detail["published_date"] or listed_date,
                        "posted_date": listed_date,
                        "authors": detail["authors"] or None,
                        "publisher": detail["publisher"],
                        "url": full_url,
                        "pdf_url": detail["pdf_url"] or None,
                        "original_filename": detail["original_filename"] or None,
                        "category": category,
                        "keywords": None,
                        "doi": None,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] p{page} item {idx} failed: {exc}")
                    continue

            # End of page: check if there are more pages
            if page >= (total_pages or 1):
                print(f"[{self.site_id}] Reached last page ({page}); stopping")
                break
            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
