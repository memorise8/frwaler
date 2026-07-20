# -*- coding: utf-8 -*-
"""Crawler for INERIS publications via the public HAL archives-ouvertes.fr API.

Starting URL:
  https://ineris.hal.science/search/index/?q=%2A&rows=30&submitType_s=file&docType_s=COMM+OR+ART+OR+THESE

Data source:
  https://api.archives-ouvertes.fr/search/ (public Solr API, no auth needed)
  Filtered to halId_s:ineris-* with docType_s:(COMM OR ART OR THESE) and submitType_s:file.
  Returns ~2 325 records as of 2026-05-13.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

# Absolute import — spec_from_file_location has no package context
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from crawler.base_crawler import BaseCrawler

_API_URL = "https://api.archives-ouvertes.fr/search/"
_FIELDS = (
    "docid,halId_s,title_s,abstract_s,authFullName_s,"
    "producedDate_s,submittedDate_s,docType_s,"
    "keyword_s,fileMain_s,linkExtUrl_s,doiId_s,journalTitle_s,"
    "labStructName_s,instStructAcronym_s,domain_s,language_s,"
    "series_s,volume_s,issue_s"
)
_ROWS = 30
_MAX_PAGES = 200
_CRAWL_BUDGET_SECS = 25 * 60
_MIN_ABSTRACT_LEN = 50


class InerisHalScienceSearchCrawler(BaseCrawler):
    site_id = "ineris-hal-science-search"
    site_name = "Custom: ineris-hal-science-search"
    base_url = "https://ineris.hal.science"

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, params):
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

    def _fetch_page(self, start, rows):
        """Fetch one page from the HAL Solr API. Returns parsed JSON or None."""
        params = {
            "q": "halId_s:ineris-*",
            "rows": rows,
            "start": start,
            "wt": "json",
            "fl": _FIELDS,
            "sort": "docid asc",
            "fq": [
                "docType_s:(COMM OR ART OR THESE)",
                "submitType_s:file",
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
    def _parse_date(raw):
        """Normalise various HAL date formats to YYYY-MM-DD."""
        if not raw:
            return ""
        s = str(raw).strip()
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
    def _extract_filename(url):
        """Pull the last path segment from a URL as the original filename."""
        if not url:
            return ""
        try:
            path = urllib.parse.urlparse(url).path
            seg = path.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
            seg = urllib.parse.unquote(seg)
            return seg if "." in seg else ""
        except Exception:
            return ""

    @staticmethod
    def _strip_html(text):
        """Remove HTML tags and normalise whitespace."""
        if not text:
            return ""
        clean = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", clean).strip()

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        limit_str = str(limit) if limit is not None else "inf"
        saved = 0
        seen_ids = set()
        start_offset = 0
        start_time = time.time()

        for page in range(_MAX_PAGES):
            if time.time() - start_time > _CRAWL_BUDGET_SECS:
                print(f"[{self.site_id}] 25-minute budget reached after {page} pages. Exiting.")
                break

            if limit is not None and saved >= limit:
                break

            if page == _MAX_PAGES - 1:
                print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached. Logging and exiting.")

            if page % 10 == 0 and page > 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            rows_this_page = _ROWS
            if limit is not None:
                rows_this_page = min(_ROWS, limit - saved)

            time.sleep(self._delay)

            data = self._fetch_page(start_offset, rows_this_page)
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
                    docid = str(doc.get("docid") or "")

                    # ID-based deduplication guards against pagination loops
                    dedup_key = hal_id or docid
                    if dedup_key in seen_ids:
                        continue
                    seen_ids.add(dedup_key)

                    # Title
                    title_list = doc.get("title_s") or []
                    # Bilingual records: first=FR, last=EN — prefer English when available
                    title = (title_list[-1] if len(title_list) > 1 else title_list[0]).strip() if title_list else ""
                    if not title:
                        print(f"[{self.site_id}] Skipping doc {docid}: no title")
                        continue

                    # Abstract — join all language variants; strip stray HTML
                    abstract_parts = [
                        self._strip_html(a) for a in (doc.get("abstract_s") or [])
                        if a and a.strip()
                    ]
                    abstract = "\n\n".join(p for p in abstract_parts if p)
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
                    produced_date = self._parse_date(doc.get("producedDate_s"))
                    submitted_raw = doc.get("submittedDate_s") or ""
                    posted_date = self._parse_date(submitted_raw)

                    # Document type
                    doc_type = str(doc.get("docType_s") or "")

                    # Journal
                    journal = str(doc.get("journalTitle_s") or "")

                    # DOI — HAL uses doiId_s
                    doi = str(doc.get("doiId_s") or "")

                    # Publisher — lab/institution names
                    lab_list = doc.get("labStructName_s") or []
                    publisher = "; ".join(str(x) for x in lab_list if x)

                    # Department — institution acronyms
                    inst_acro_list = doc.get("instStructAcronym_s") or []
                    department = "; ".join(str(a) for a in inst_acro_list) if inst_acro_list else ""

                    # PDF URL and original filename
                    file_main = str(doc.get("fileMain_s") or "")
                    link_ext = str(doc.get("linkExtUrl_s") or "")
                    pdf_url = file_main or link_ext
                    original_filename = self._extract_filename(pdf_url)

                    # Detail page URL
                    url = f"{self.base_url}/{hal_id}" if hal_id else ""

                    # issue may be a list
                    issue_raw = doc.get("issue_s")
                    if isinstance(issue_raw, list):
                        issue_raw = issue_raw[0] if issue_raw else None

                    metadata = {
                        "posted_date": posted_date or None,
                        "originalFilename": original_filename or None,
                        "journal_raw": journal or None,
                        "series": doc.get("series_s") or None,
                        "volume": doc.get("volume_s") or None,
                        "issue": issue_raw,
                        "halId": hal_id or None,
                        "docid": docid or None,
                        "docType": doc_type or None,
                        "domains": doc.get("domain_s") or None,
                        "language": doc.get("language_s") or None,
                        "producedDate_raw": str(doc.get("producedDate_s") or "") or None,
                        "submittedDate_raw": submitted_raw or None,
                    }
                    metadata = {k: v for k, v in metadata.items() if v is not None}

                    paper = {
                        "site_id": self.site_id,
                        "external_id": hal_id or docid,
                        "post_number": docid,
                        "title": title,
                        "abstract": abstract,
                        "published_date": produced_date,
                        "posted_date": posted_date,
                        "authors": authors,
                        "publisher": publisher,
                        "department": department,
                        "journal": journal,
                        "url": url,
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

            # Advance offset by actual docs returned (not always == rows_this_page)
            start_offset += len(docs)

            # All items on the page were duplicates → pagination has looped
            if new_on_page == 0 and page > 0:
                print(
                    f"[{self.site_id}] No new records on page {page} "
                    "(all seen or skipped). Stopping."
                )
                break

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
