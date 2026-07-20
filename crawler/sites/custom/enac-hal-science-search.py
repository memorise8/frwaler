# -*- coding: utf-8 -*-
"""Crawler for ENAC HAL Science - OTHER and REPORT docs with attached files.

Starting URL reference:
  https://enac.hal.science/search/index/?q=%2A&rows=30&submitType_s=file&docType_s=OTHER+OR+REPORT

Uses the public HAL Solr portal API (api.archives-ouvertes.fr/search/enac/) to
collect REPORT and OTHER document types with attached files from ENAC.
"""

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

_API_URL = "https://api.archives-ouvertes.fr/search/enac/"
_FIELDS = ",".join([
    "docid", "halId_s", "uri_s",
    "title_s",
    "abstract_s", "en_abstract_s", "fr_abstract_s",
    "authFullName_s",
    "producedDate_s", "submittedDate_s",
    "docType_s",
    "keyword_s",
    "fileMain_s",
    "doi_s", "journalTitle_s", "publisher_s",
    "instStructName_s", "labStructName_s",
    "domain_s", "language_s",
    "volume_s", "issue_s",
])
_ROWS = 30
_MAX_PAGES = 200
_CRAWL_BUDGET_SECS = 25 * 60
_MIN_ABSTRACT_LEN = 100


class EnacHalScienceSearchCrawler(BaseCrawler):
    site_id = "enac-hal-science-search"
    site_name = "Custom: enac-hal-science-search"
    base_url = "https://enac.hal.science"

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, params):
        """GET via curl with retry/backoff; returns raw text or None."""
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
                    print(f"[{self.site_id}] Empty response, retrying in {wait}s…")
                    time.sleep(wait)
            except Exception as exc:
                wait = 1 * (3 ** attempt)
                if attempt < 2:
                    print(f"[{self.site_id}] curl error: {exc}, retrying in {wait}s…")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return None

    def _fetch_page(self, start, rows):
        """Fetch one page from the HAL Solr API. Returns parsed JSON or None."""
        params = {
            "q": "*",
            "rows": rows,
            "start": start,
            "wt": "json",
            "fl": _FIELDS,
            "fq": [
                "submitType_s:file",
                "docType_s:(OTHER OR REPORT)",
            ],
            "sort": "docid asc",
        }
        raw = self._curl_get(_API_URL, params)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] JSON parse error at start={start}: {exc}")
            return None

    def _get_pdf_filename(self, pdf_url, hal_id):
        """Get original filename from Content-Disposition header or URL path."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "-I",
            "--max-time", "10",
            "-H", f"User-Agent: {self.USER_AGENT}",
            pdf_url,
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=15, errors="replace",
            )
            for line in (result.stdout or "").splitlines():
                if line.lower().startswith("content-disposition:"):
                    m = re.search(
                        r'filename[^;=\n]*=\s*["\']?([^"\';\r\n]+)',
                        line, re.IGNORECASE,
                    )
                    if m:
                        name = m.group(1).strip().strip('"\'')
                        if name:
                            return name
        except Exception:
            pass
        # Fallback: last meaningful URL path segment
        try:
            path = urllib.parse.urlparse(pdf_url).path
            seg = path.rstrip("/").split("/")[-1].split("?")[0]
            if seg and "." in seg:
                return seg
        except Exception:
            pass
        return f"{hal_id}.pdf" if hal_id else ""

    # ------------------------------------------------------------------
    # Parsing utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_date(raw):
        """Normalise HAL date variants to YYYY-MM-DD (or shorter)."""
        if not raw:
            return ""
        s = str(raw)
        m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
        if m:
            return m.group(1)
        m = re.match(r"(\d{4}-\d{2})$", s)
        if m:
            return m.group(1) + "-01"
        m = re.match(r"(\d{4})$", s)
        if m:
            return m.group(1) + "-01-01"
        return s[:10] if len(s) >= 10 else s

    @staticmethod
    def _pick_abstract(doc):
        """Return the longest non-empty abstract across language variants."""
        best = ""
        for key in ("abstract_s", "en_abstract_s", "fr_abstract_s"):
            val = doc.get(key)
            if isinstance(val, list):
                val = val[0] if val else ""
            if val and len(str(val)) > len(best):
                best = str(val).strip()
        return best

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        limit_str = str(limit) if limit is not None else "∞"
        saved = 0
        seen_ids: set = set()
        start_time = time.time()

        for page in range(_MAX_PAGES):
            # Wall-clock budget guard
            if time.time() - start_time > _CRAWL_BUDGET_SECS:
                print(f"[{self.site_id}] 25-minute budget reached after {page} pages. Exiting.")
                break

            if limit is not None and saved >= limit:
                break

            if page == _MAX_PAGES - 1:
                print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached. Exiting.")

            if page % 10 == 0:
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
                    hal_id = str(doc.get("halId_s") or "")
                    docid = str(doc.get("docid") or "")
                    uri = str(doc.get("uri_s") or "")

                    # Deduplicate by halId then docid
                    dedup_key = hal_id or docid
                    if not dedup_key:
                        continue
                    if dedup_key in seen_ids:
                        continue
                    seen_ids.add(dedup_key)

                    # Build detail page URL
                    url = uri or (f"{self.base_url}/{hal_id}" if hal_id else "")

                    # Title
                    title_list = doc.get("title_s") or []
                    title = title_list[0].strip() if title_list else ""
                    if not title:
                        print(f"[{self.site_id}] Skipping doc {docid or hal_id}: no title")
                        continue

                    # Abstract — pick longest, skip if too short
                    abstract = self._pick_abstract(doc)
                    if len(abstract) < _MIN_ABSTRACT_LEN:
                        print(
                            f"[{self.site_id}] Skipping doc {docid or hal_id}: "
                            f"abstract too short ({len(abstract)} chars)"
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

                    # Category / doc type
                    doc_type = str(doc.get("docType_s") or "")

                    # Journal / publication info
                    journal = str(doc.get("journalTitle_s") or "")
                    volume = str(doc.get("volume_s") or "")
                    issue = str(doc.get("issue_s") or "")

                    # DOI
                    doi = str(doc.get("doi_s") or "")

                    # Publisher — prefer explicit publisher_s, fall back to institution
                    publisher_raw = doc.get("publisher_s") or []
                    inst_list = doc.get("instStructName_s") or []
                    if publisher_raw:
                        publisher = "; ".join(str(p) for p in publisher_raw)
                    else:
                        publisher = "; ".join(str(i) for i in inst_list[:3])

                    # PDF URL
                    pdf_url = str(doc.get("fileMain_s") or "")

                    # post_number: numeric part of halId (e.g. "hal-00874397" → "00874397")
                    post_number = ""
                    if hal_id and "-" in hal_id:
                        post_number = hal_id.split("-", 1)[-1]
                    if not post_number:
                        post_number = docid

                    # Original filename: try Content-Disposition header
                    original_filename = ""
                    if pdf_url:
                        original_filename = self._get_pdf_filename(pdf_url, hal_id)

                    # Metadata: all remaining raw fields
                    domain_list = doc.get("domain_s") or []
                    lab_list = doc.get("labStructName_s") or []
                    lang = str(doc.get("language_s") or "")

                    metadata = {
                        "posted_date": submitted_raw,
                        "originalFilename": original_filename,
                        "journal_raw": journal,
                        "volume": volume,
                        "issue": issue,
                        "halId": hal_id,
                        "docid": docid,
                        "docType": doc_type,
                        "domains": domain_list,
                        "labStructName": lab_list,
                        "instStructName": [str(i) for i in inst_list],
                        "language": lang,
                    }

                    paper = {
                        "site_id": self.site_id,
                        "external_id": hal_id or docid,
                        "post_number": post_number or None,
                        "title": title,
                        "abstract": abstract,
                        "published_date": produced_date or None,
                        "posted_date": posted_date or None,
                        "authors": authors or None,
                        "publisher": publisher or None,
                        "journal": journal or None,
                        "url": url,
                        "pdf_url": pdf_url or None,
                        "keywords": keywords or None,
                        "category": doc_type or None,
                        "doi": doi or None,
                        "original_filename": original_filename or None,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    new_on_page += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:70]}")

                    time.sleep(self._delay)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {doc.get('docid', '?')} failed: {exc}")
                    continue

            # If an entire page produced zero new records, pagination looped back
            if new_on_page == 0 and page > 0:
                print(
                    f"[{self.site_id}] No new records on page {page} "
                    "(all skipped or already seen). Stopping."
                )
                break

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
