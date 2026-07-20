# -*- coding: utf-8 -*-
"""Crawler for ENS Lyon records in HAL Science (REPORT and OTHER with files).

Starting URL reference:
  https://ens-lyon.hal.science/search/index/?q=%2A&rows=30&submitType_s=file&docType_s=REPORT+OR+OTHER

The portal (ens-lyon.hal.science) is protected by Anubis bot-detection, so we
use the public HAL Solr REST API (api.archives-ouvertes.fr/search/) which is
unprotected and returns JSON directly.  ENS Lyon content is identified via the
collection code ``collCode_s:ENS-LYON``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from crawler.base_crawler import BaseCrawler

_API_URL = "https://api.archives-ouvertes.fr/search/"

# All fields fetched in a single API call — no per-item detail fetch needed.
_FIELDS = ",".join((
    "docid",
    "halId_s",
    "uri_s",
    "title_s",
    "abstract_s",
    "authFullName_s",
    "producedDate_s",
    "submittedDate_s",
    "docType_s",
    "keyword_s",
    "fileMain_s",
    "files_s",
    "doiId_s",
    "journalTitle_s",
    "publisher_s",
    "instStructName_s",
    "labStructName_s",
    "domain_s",
    "language_s",
    "reportNumber_s",
))

_ROWS = 30
_MAX_PAGES = 200
_CRAWL_BUDGET_SECS = 25 * 60
_MIN_ABSTRACT_LEN = 100


class EnsLyonHalScienceSearchCrawler(BaseCrawler):
    site_id = "ens-lyon-hal-science-search"
    site_name = "Custom: ens-lyon-hal-science-search"
    base_url = "https://ens-lyon.hal.science"

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, params: dict) -> str | None:
        """GET via curl with retry/backoff; returns raw response text or None."""
        qs = urllib.parse.urlencode(params, doseq=True)
        full_url = f"{url}?{qs}"
        cmd = [
            "curl", "--tls-max", "1.3", "-sk",
            "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            full_url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=35,
                    errors="replace",
                )
                if result.stdout.strip():
                    return result.stdout
                wait = 1 * (3 ** attempt)
                if attempt < 2:
                    print(f"[{self.site_id}] Empty response, retrying in {wait}s...")
                    time.sleep(wait)
            except Exception as exc:
                wait = 1 * (3 ** attempt)
                if attempt < 2:
                    print(f"[{self.site_id}] curl error: {exc}, retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return None

    def _fetch_page(self, start: int, rows: int) -> dict | None:
        """Fetch one page from the HAL Solr API. Returns parsed JSON or None."""
        params = {
            "q": "*",
            "rows": rows,
            "start": start,
            "wt": "json",
            "fl": _FIELDS,
            # Multiple fq values — Solr AND-s them
            "fq": [
                "collCode_s:ENS-LYON",
                "submitType_s:file",
                "docType_s:(REPORT OR OTHER)",
            ],
        }
        raw = self._curl_get(_API_URL, params)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] JSON parse error at start={start}: {exc}")
            return None

    # ------------------------------------------------------------------
    # Parsing utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_date(raw: str) -> str:
        """Normalise various HAL date formats to YYYY-MM-DD."""
        if not raw:
            return ""
        m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
        if m:
            return m.group(1)
        m = re.match(r"(\d{4}-\d{2})$", raw)
        if m:
            return m.group(1) + "-01"
        m = re.match(r"(\d{4})$", raw)
        if m:
            return m.group(1) + "-01-01"
        return raw[:10] if len(raw) >= 10 else raw

    @staticmethod
    def _extract_filename(url: str) -> str:
        """Pull the last path segment from a URL as the original filename."""
        if not url:
            return ""
        try:
            path = urllib.parse.urlparse(url).path
            seg = path.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
            return seg if "." in seg else ""
        except Exception:
            return ""

    @staticmethod
    def _post_number(hal_id: str, docid) -> str:
        """Return numeric post_number: prefer docid (pure int), else trailing
        digits of hal_id, else the hal_id itself."""
        docid_str = str(docid).strip() if docid else ""
        if docid_str and docid_str.isdigit():
            return docid_str
        m = re.search(r"(\d+)$", hal_id or "")
        return m.group(1) if m else (hal_id or "")

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        limit_str = str(limit) if limit is not None else "∞"
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()

        for page in range(_MAX_PAGES):
            # Wall-clock budget guard
            if time.time() - start_time > _CRAWL_BUDGET_SECS:
                print(f"[{self.site_id}] 25-minute budget reached after {page} pages. Exiting.")
                break

            if limit is not None and saved >= limit:
                break

            if page == _MAX_PAGES - 1:
                print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached. Logging and exiting.")

            # Progress log every 10 pages
            if page % 10 == 0 and page > 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            start_offset = page * _ROWS
            data = self._fetch_page(start_offset, _ROWS)
            if data is None:
                print(f"[{self.site_id}] Failed to fetch page {page}. Stopping.")
                break

            docs = data.get("response", {}).get("docs", [])
            if not docs:
                print(f"[{self.site_id}] No more documents at page {page} (start={start_offset}). Done.")
                break

            if page == 0:
                total = data.get("response", {}).get("numFound", "?")
                print(f"[{self.site_id}] Total available on server: {total}")

            new_on_page = 0

            for doc in docs:
                if limit is not None and saved >= limit:
                    break

                try:
                    hal_id = doc.get("halId_s") or ""
                    docid = doc.get("docid")
                    uri = str(doc.get("uri_s") or "")
                    if not uri and hal_id:
                        uri = f"https://hal.science/{hal_id}"

                    # URL-based deduplication
                    dedup_key = uri or hal_id or str(docid)
                    if dedup_key in seen_urls:
                        continue
                    seen_urls.add(dedup_key)

                    # Title
                    title_list = doc.get("title_s") or []
                    title = title_list[0].strip() if title_list else ""
                    if not title:
                        print(f"[{self.site_id}] Skipping doc {docid}: no title")
                        continue

                    # Abstract — skip if too short
                    abstract_list = doc.get("abstract_s") or []
                    abstract = abstract_list[0].strip() if abstract_list else ""
                    if len(abstract) < _MIN_ABSTRACT_LEN:
                        print(
                            f"[{self.site_id}] Skipping doc {docid}: abstract too short "
                            f"({len(abstract)} chars)"
                        )
                        continue

                    # Authors
                    authors_list = doc.get("authFullName_s") or []
                    authors = "; ".join(str(a) for a in authors_list)

                    # Keywords
                    keywords_list = doc.get("keyword_s") or []
                    keywords = ", ".join(str(k) for k in keywords_list)

                    # Dates
                    produced_date = self._parse_date(str(doc.get("producedDate_s") or ""))
                    submitted_raw = str(doc.get("submittedDate_s") or "")
                    posted_date = self._parse_date(submitted_raw)

                    # Document type / category
                    doc_type = str(doc.get("docType_s") or "")

                    # Journal
                    journal = str(doc.get("journalTitle_s") or "")

                    # DOI
                    doi = str(doc.get("doiId_s") or "")

                    # Publisher — prefer explicit publisher, fall back to institution
                    publisher_list = doc.get("publisher_s") or []
                    inst_list = doc.get("instStructName_s") or []
                    lab_list = doc.get("labStructName_s") or []
                    if publisher_list:
                        publisher = "; ".join(str(p) for p in publisher_list)
                    elif lab_list:
                        publisher = "; ".join(str(p) for p in lab_list[:3])
                    else:
                        publisher = "; ".join(str(i) for i in inst_list[:3])

                    # PDF URL and filename
                    files_list = doc.get("files_s") or []
                    pdf_url = str(doc.get("fileMain_s") or (files_list[0] if files_list else ""))
                    original_filename = self._extract_filename(
                        files_list[0] if files_list else pdf_url
                    )

                    # Extra metadata
                    domain_list = doc.get("domain_s") or []
                    lang = str(doc.get("language_s") or "")
                    report_number = str(doc.get("reportNumber_s") or "")

                    metadata = {
                        "posted_date": submitted_raw,
                        "originalFilename": original_filename,
                        "halId": hal_id,
                        "docid": str(docid) if docid else "",
                        "docType": doc_type,
                        "domains": domain_list,
                        "labStructName": lab_list,
                        "instStructName": [str(i) for i in inst_list],
                        "language": lang,
                        "report_number": report_number,
                        "journal_raw": journal,
                    }

                    paper = {
                        "site_id": self.site_id,
                        "external_id": hal_id or str(docid),
                        "post_number": self._post_number(hal_id, docid),
                        "title": title,
                        "abstract": abstract,
                        "published_date": produced_date,
                        "posted_date": posted_date,
                        "authors": authors,
                        "publisher": publisher,
                        "journal": journal,
                        "url": uri,
                        "pdf_url": pdf_url,
                        "keywords": keywords,
                        "category": doc_type,
                        "doi": doi,
                        "original_filename": original_filename,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    new_on_page += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {doc.get('docid', '?')} failed: {exc}")
                    continue

            # If an entire page produced zero new records, pagination has looped
            if new_on_page == 0 and page > 0:
                print(
                    f"[{self.site_id}] No new records on page {page} "
                    "(all skipped or already seen). Stopping."
                )
                break

            time.sleep(self._delay)

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
