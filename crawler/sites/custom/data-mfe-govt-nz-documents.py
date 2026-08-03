# -*- coding: utf-8 -*-
"""Crawler for MfE Data Service documents (data.mfe.govt.nz/documents/).

Uses the Koordinates REST API v1.x:
  GET /services/api/v1.x/documents/?page_size=100&page=N  → list
  GET /services/api/v1.x/documents/{id}/                  → detail

Absolute import — loaded via spec_from_file_location (no package context).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_THIS_DIR)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402

_API_BASE = "https://data.mfe.govt.nz/services/api/v1.x"
_PAGE_SIZE = 100
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_WALL_BUDGET = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds
_ABSTRACT_MIN = 100     # skip items whose abstract is shorter (test asserts >= 100)
_PUBLISHER = "Ministry for the Environment"


class DataMfeGovtNzDocumentsCrawler(BaseCrawler):
    site_id = "data-mfe-govt-nz-documents"
    site_name = "Custom: data-mfe-govt-nz-documents"
    base_url = "https://data.mfe.govt.nz"

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, retries: int = 3) -> str | None:
        """GET via curl; returns decoded response body or None after retries."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
            "-A", self.USER_AGENT,
            "-H", "Accept: application/json, */*",
            "-H", "Accept-Language: en-NZ,en;q=0.9",
            url,
        ]
        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt+1}/{retries}): {exc}")
            if attempt < retries - 1:
                wait = [1, 3, 9][attempt]
                print(f"[{self.site_id}] Retrying in {wait}s...")
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_html(html_text: str) -> str:
        if not html_text:
            return ""
        text = re.sub(r"<[^>]+>", " ", html_text)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&amp;", "&", text)
        text = re.sub(r"&lt;", "<", text)
        text = re.sub(r"&gt;", ">", text)
        text = re.sub(r"&[a-zA-Z#\d]+;", "", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _parse_date(iso_str: str | None) -> str | None:
        if not iso_str:
            return None
        m = re.match(r"(\d{4}-\d{2}-\d{2})", str(iso_str))
        return m.group(1) if m else None

    @staticmethod
    def _original_filename(url_html: str | None, extension: str | None) -> str | None:
        """Derive original filename from the HTML page URL slug + file extension.

        e.g. .../document/11123-river-env-guide-2010/  + ext=pdf
             → river-env-guide-2010.pdf
        """
        if not url_html or not extension:
            return None
        # URL pattern: /document/{id}-{slug}/
        m = re.search(r"/document/\d+-([^/]+?)/?$", url_html)
        if not m:
            return None
        return f"{m.group(1)}.{extension}"

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        saved = 0
        seen_ids: set[str] = set()
        start_time = time.time()

        for page in range(1, _MAX_PAGES + 1):
            # Wall-clock budget check
            if time.time() - start_time > _WALL_BUDGET:
                print(f"[{self.site_id}] Wall-clock budget exceeded. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page == _MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break

            list_url = f"{_API_BASE}/documents/?page_size={_PAGE_SIZE}&page={page}"

            try:
                raw = self._curl_get(list_url)
                if not raw:
                    print(f"[{self.site_id}] Failed to fetch list page {page}. Stopping.")
                    break

                items = json.loads(raw)
                if not isinstance(items, list):
                    # API returned an error dict or unexpected structure
                    print(f"[{self.site_id}] Unexpected response at page {page} "
                          f"(type={type(items).__name__}). Stopping.")
                    break
                if not items:
                    print(f"[{self.site_id}] No more items at page {page}. Done.")
                    break

            except json.JSONDecodeError as exc:
                print(f"[{self.site_id}] JSON decode error at page {page}: {exc}. Stopping.")
                break

            if page % 10 == 0:
                limit_str = str(limit) if limit is not None else "inf"
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            for item in items:
                if limit is not None and saved >= limit:
                    break

                if time.time() - start_time > _WALL_BUDGET:
                    break

                doc_id = item.get("id")
                if not doc_id:
                    continue

                id_str = str(doc_id)
                if id_str in seen_ids:
                    continue
                seen_ids.add(id_str)

                try:
                    time.sleep(self._delay)

                    detail_raw = self._curl_get(f"{_API_BASE}/documents/{doc_id}/")
                    if not detail_raw:
                        print(f"[{self.site_id}] item {doc_id}: no detail response. Skipping.")
                        continue

                    detail = json.loads(detail_raw)

                    title = (detail.get("title") or "").strip()
                    if not title:
                        print(f"[{self.site_id}] item {doc_id}: no title. Skipping.")
                        continue

                    # Abstract: prefer plain text description, fall back to stripped HTML
                    description = (detail.get("description") or "").strip()
                    if not description:
                        description = self._strip_html(detail.get("description_html") or "")

                    if len(description) < _ABSTRACT_MIN:
                        print(f"[{self.site_id}] item {doc_id}: abstract too short "
                              f"({len(description)} chars). Skipping.")
                        continue

                    # Dates
                    published_date = self._parse_date(detail.get("first_published_at"))
                    posted_date = self._parse_date(detail.get("published_at"))

                    # URLs
                    url_html = (detail.get("url_html")
                                or f"https://data.mfe.govt.nz/document/{doc_id}/")
                    url_download = detail.get("url_download") or ""
                    extension = (detail.get("extension") or "").lower()
                    pdf_url = url_download if extension == "pdf" else None
                    original_filename = self._original_filename(url_html, extension)

                    # Taxonomy
                    categories = detail.get("categories") or []
                    category = ", ".join(
                        c.get("name", "") for c in categories if c.get("name")
                    )
                    tags = detail.get("tags") or []
                    keywords = ", ".join(tags) if tags else None

                    # Site-specific metadata
                    group_info = detail.get("group") or {}
                    license_info = detail.get("license") or {}
                    version_info = detail.get("version") or {}
                    metadata_dict = {
                        "posted_date": detail.get("published_at"),
                        "originalFilename": original_filename,
                        "file_size": detail.get("file_size"),
                        "file_size_formatted": detail.get("file_size_formatted"),
                        "extension": extension,
                        "num_views": detail.get("num_views"),
                        "num_downloads": detail.get("num_downloads"),
                        "group_name": group_info.get("name"),
                        "public_access": detail.get("public_access"),
                        "license": license_info.get("title"),
                        "url_download": url_download,
                        "version_id": version_info.get("id"),
                        "node_id": doc_id,
                    }

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": id_str,
                        "post_number": id_str,
                        "title": title,
                        "abstract": description,
                        "published_date": published_date,
                        "posted_date": posted_date,
                        "url": url_html,
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "keywords": keywords,
                        "category": category,
                        "publisher": _PUBLISHER,
                        "authors": None,
                        "department": None,
                        "doi": None,
                        "journal": None,
                        "metadata": json.dumps(metadata_dict, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    limit_str = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {doc_id} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
