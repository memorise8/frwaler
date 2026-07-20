# -*- coding: utf-8 -*-
"""DOAJ (Directory of Open Access Journals) article search crawler.

Starting URL: https://doaj.org/search/articles?source=...
API endpoint: https://doaj.org/api/search/articles/*?pageSize=50&page=N&sort=created_date:desc
"""

import json
import os
import subprocess
import sys
import time
from urllib.parse import urlencode

# Absolute import: four levels up from crawler/sites/custom/<file> → project root
_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(
        os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        )
    )
)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402

_API_BASE = "https://doaj.org/api/search/articles"
_PAGE_SIZE = 50
_MAX_PAGES = 200


class DOAJSearchCrawler(BaseCrawler):
    """Crawler for DOAJ article search (newest-first by created_date)."""

    site_id = "doaj-org-search"
    site_name = "Custom: doaj-org-search"
    base_url = "https://doaj.org"

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, retries: int = 3):
        """GET via curl with exponential-backoff retries. Returns body str or None."""
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: application/json",
            url,
        ]
        delays = [1, 3, 9]
        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace").strip()
                if text:
                    return text
                if attempt < retries - 1:
                    wait = delays[attempt]
                    print(f"[{self.site_id}] Empty response (attempt {attempt + 1}), retry in {wait}s...")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < retries - 1:
                    wait = delays[attempt]
                    print(f"[{self.site_id}] curl error: {exc}, retry in {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after {retries} attempts: {exc}")
        return None

    @staticmethod
    def _build_url(page: int) -> str:
        params = {"pageSize": _PAGE_SIZE, "page": page, "sort": "created_date:desc"}
        return f"{_API_BASE}/*?{urlencode(params)}"

    # ------------------------------------------------------------------
    # Field parsers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_doi(bibjson: dict) -> str:
        for ident in bibjson.get("identifier") or []:
            if ident.get("type") == "doi":
                return ident.get("id") or ""
        return ""

    @staticmethod
    def _get_pdf_url(bibjson: dict) -> str:
        # First pass: explicit PDF content_type or type
        for link in bibjson.get("link") or []:
            ct = (link.get("content_type") or "").upper()
            lt = (link.get("type") or "").lower()
            url = link.get("url") or ""
            if "PDF" in ct or "pdf" in lt:
                return url
        # Second pass: URL ends in .pdf
        for link in bibjson.get("link") or []:
            url = link.get("url") or ""
            if url.lower().endswith(".pdf"):
                return url
        return ""

    @staticmethod
    def _make_published_date(bibjson: dict) -> str:
        year = bibjson.get("year") or ""
        month = bibjson.get("month") or ""
        day = bibjson.get("day") or ""
        try:
            if year and month and day:
                return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
            if year and month:
                return f"{int(year):04d}-{int(month):02d}-01"
            if year:
                return f"{int(year):04d}-01-01"
        except (ValueError, TypeError):
            pass
        return ""

    @staticmethod
    def _original_filename(pdf_url: str) -> str:
        if not pdf_url:
            return ""
        seg = pdf_url.rstrip("/").split("?")[0].split("/")[-1]
        return seg if "." in seg else ""

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_ids: set = set()
        start_time = time.time()
        max_wall = 25 * 60  # 25-minute safety budget

        for page in range(1, _MAX_PAGES + 1):
            if time.time() - start_time > max_wall:
                print(f"[{self.site_id}] Approaching 25-minute budget, stopping cleanly.")
                break

            if limit is not None and saved >= limit:
                break

            url = self._build_url(page)
            raw = self._curl_get(url)
            if not raw:
                print(f"[{self.site_id}] Failed to fetch page {page}. Stopping.")
                break

            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"[{self.site_id}] JSON decode error at page {page}: {exc}. Stopping.")
                break

            results = data.get("results") or []
            if not results:
                print(f"[{self.site_id}] No results at page {page}. Done.")
                break

            if page == 1:
                total = data.get("total", "?")
                print(f"[{self.site_id}] Total articles in DOAJ: {total}")

            if page % 10 == 0:
                lim_str = str(limit) if limit is not None else "inf"
                print(f"[{self.site_id}] page {page}: saved {saved}/{lim_str}")

            for result in results:
                if limit is not None and saved >= limit:
                    break

                try:
                    article_id = result.get("id") or ""
                    if article_id in seen_ids:
                        continue
                    if article_id:
                        seen_ids.add(article_id)

                    created_date = result.get("created_date") or ""
                    last_updated = result.get("last_updated") or ""
                    bibjson = result.get("bibjson") or {}

                    title = (bibjson.get("title") or "").strip()
                    abstract = (bibjson.get("abstract") or "").strip()

                    if not title:
                        print(f"[{self.site_id}] Skipping {article_id}: no title")
                        continue

                    if len(abstract) < 100:
                        print(
                            f"[{self.site_id}] Skipping {article_id}: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    # Authors
                    authors_list = [
                        a.get("name") or ""
                        for a in (bibjson.get("author") or [])
                        if a.get("name")
                    ]
                    authors_str = "; ".join(authors_list)

                    # Journal & publisher
                    journal_obj = bibjson.get("journal") or {}
                    journal_title = journal_obj.get("title") or ""
                    publisher = journal_obj.get("publisher") or ""
                    volume = journal_obj.get("volume") or ""
                    issue = journal_obj.get("number") or ""

                    # Identifiers
                    doi = self._get_doi(bibjson)

                    # URLs
                    detail_url = f"https://doaj.org/article/{article_id}" if article_id else ""
                    pdf_url = self._get_pdf_url(bibjson)

                    # Keywords (DOAJ provides as a list)
                    kws = [k.strip() for k in (bibjson.get("keywords") or []) if k.strip()]
                    keywords_str = ", ".join(kws)

                    # Category (first subject term)
                    subjects = bibjson.get("subject") or []
                    category = subjects[0].get("term") if subjects else ""

                    # Dates
                    published_date = self._make_published_date(bibjson)
                    listed_date = created_date[:10] if len(created_date) >= 10 else ""

                    # original_filename from PDF URL
                    original_filename = self._original_filename(pdf_url) if pdf_url else ""

                    # Affiliations for metadata
                    affiliations = [
                        a.get("affiliation") or ""
                        for a in (bibjson.get("author") or [])
                        if a.get("affiliation")
                    ]

                    metadata = {
                        "posted_date": created_date,
                        "last_updated": last_updated,
                        "journal_raw": journal_title,
                        "volume": volume,
                        "issue": issue,
                        "start_page": bibjson.get("start_page") or "",
                        "end_page": bibjson.get("end_page") or "",
                        "affiliations": affiliations,
                        "subjects": [s.get("term") for s in subjects if s.get("term")],
                        "issns": journal_obj.get("issns") or [],
                        "country": journal_obj.get("country") or "",
                        "language": journal_obj.get("language") or [],
                        "orcid_ids": [
                            a.get("orcid_id")
                            for a in (bibjson.get("author") or [])
                            if a.get("orcid_id")
                        ],
                        "originalFilename": original_filename,
                    }

                    paper = {
                        "site_id": self.site_id,
                        "external_id": article_id,
                        "url": detail_url,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": listed_date,
                        "authors": authors_str,
                        "publisher": publisher,
                        "journal": journal_title,
                        "pdf_url": pdf_url or None,
                        "keywords": keywords_str,
                        "category": category,
                        "doi": doi,
                        "original_filename": original_filename or None,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1

                    lim_str = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] Saved {saved}/{lim_str}: {title[:60]}")

                    time.sleep(self._delay * 0.1)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {result.get('id', '?')} failed: {exc}")
                    continue

            time.sleep(self._delay * 0.3)

            if page == _MAX_PAGES:
                print(f"[{self.site_id}] Reached safety cap of {_MAX_PAGES} pages.")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
