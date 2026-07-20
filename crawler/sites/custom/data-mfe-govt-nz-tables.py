# -*- coding: utf-8 -*-
"""Crawler for MfE Data Service tables (data.mfe.govt.nz/tables/).

Uses the Koordinates REST API v1.x:
  GET /services/api/v1.x/tables/?format=json&limit=100&page=N  → list
  GET /services/api/v1.x/tables/{id}/?format=json              → detail
  GET /services/api/v1.x/tables/{id}/versions/{vid}/attachments/?format=json → PDF attachments

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
_MAX_PAGES = 200
_WALL_BUDGET = 25 * 60  # seconds
_ABSTRACT_MIN = 100
_PUBLISHER = "Ministry for the Environment"


class DataMfeGovtNzTablesCrawler(BaseCrawler):
    site_id = "data-mfe-govt-nz-tables"
    site_name = "Custom: data-mfe-govt-nz-tables"
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
    def _parse_date(iso_str) -> str | None:
        if not iso_str:
            return None
        m = re.match(r"(\d{4}-\d{2}-\d{2})", str(iso_str))
        return m.group(1) if m else None

    def _fetch_pdf_attachment(self, attachments_url: str) -> tuple[str | None, str | None]:
        """Fetch attachments list; return (pdf_url, original_filename) or (None, None)."""
        raw = self._curl_get(attachments_url)
        if not raw:
            return None, None
        try:
            items = json.loads(raw)
            if not isinstance(items, list):
                return None, None
            for att in items:
                doc = att.get("document") or {}
                ext = (doc.get("extension") or "").lower()
                if ext == "pdf":
                    pdf_dl = doc.get("url_download")
                    title_raw = (doc.get("title") or "").strip()
                    orig = title_raw if title_raw.lower().endswith(".pdf") else None
                    if not orig and pdf_dl:
                        tail = pdf_dl.rstrip("/").split("/")[-1].split("?")[0]
                        if "." in tail:
                            orig = tail
                    return pdf_dl, orig
        except Exception:
            pass
        return None, None

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        saved = 0
        seen_ids: set[str] = set()
        start_time = time.time()

        for page in range(1, _MAX_PAGES + 1):
            if time.time() - start_time > _WALL_BUDGET:
                print(f"[{self.site_id}] Wall-clock budget exceeded. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page >= _MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break

            list_url = f"{_API_BASE}/tables/?format=json&limit={_PAGE_SIZE}&page={page}"

            try:
                raw = self._curl_get(list_url)
                if not raw:
                    print(f"[{self.site_id}] Failed to fetch list page {page}. Stopping.")
                    break

                items = json.loads(raw)
                if not isinstance(items, list):
                    print(f"[{self.site_id}] Unexpected list response at page {page} "
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

                    detail_raw = self._curl_get(f"{_API_BASE}/tables/{doc_id}/?format=json")
                    if not detail_raw:
                        print(f"[{self.site_id}] item {doc_id}: no detail response. Skipping.")
                        continue

                    detail = json.loads(detail_raw)

                    title = (detail.get("title") or "").strip()
                    if not title:
                        print(f"[{self.site_id}] item {doc_id}: no title. Skipping.")
                        continue

                    # Abstract: prefer plain-text description, fall back to stripped HTML
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
                                or detail.get("url_canonical")
                                or f"https://data.mfe.govt.nz/table/{doc_id}/")

                    # Taxonomy
                    categories = detail.get("categories") or []
                    category = ", ".join(
                        c.get("name", "") for c in categories if c.get("name")
                    ) or None
                    tags = detail.get("tags") or []
                    keywords = ", ".join(tags) if tags else None

                    # PDF via attachments endpoint
                    pdf_url = None
                    original_filename = None
                    version_info = detail.get("version") or {}
                    version_id = version_info.get("id")
                    if version_id:
                        att_url = (f"{_API_BASE}/tables/{doc_id}/versions/{version_id}"
                                   f"/attachments/?format=json")
                        pdf_url, original_filename = self._fetch_pdf_attachment(att_url)

                    # Extra metadata
                    data_info = detail.get("data") or {}
                    fields = [f.get("name", "") for f in (data_info.get("fields") or [])
                              if f.get("name")]
                    group_info = detail.get("group") or {}
                    license_info = detail.get("license") or {}

                    metadata_dict = {
                        "posted_date": detail.get("published_at"),
                        "originalFilename": original_filename,
                        "num_views": detail.get("num_views"),
                        "num_downloads": detail.get("num_downloads"),
                        "feature_count": data_info.get("feature_count"),
                        "fields": fields,
                        "group_name": group_info.get("name"),
                        "public_access": detail.get("public_access"),
                        "license": license_info.get("title"),
                        "kind": detail.get("kind"),
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

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
