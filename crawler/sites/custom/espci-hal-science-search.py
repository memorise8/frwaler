# -*- coding: utf-8 -*-
"""ESPCI HAL Science search crawler.

Uses the public HAL Solr API (api.archives-ouvertes.fr/search/) to collect
REPORT and OTHER document types submitted with attached files from
Ecole Superieure de Physique et de Chimie Industrielles de la Ville de Paris
(structId_i: 301585).

Starting URL reference:
  https://espci.hal.science/search/index/?q=%2A&rows=30&submitType_s=file
  &docType_s=REPORT+OR+OTHER&structId_i=301585
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

_API_URL = "https://api.archives-ouvertes.fr/search/"
_FIELDS = (
    "halId_s,docid,title_s,abstract_s,publicationDate_s,producedDate_s,"
    "authFullName_s,structName_s,doi_s,keyword_s,fileMain_s,files_s,"
    "uri_s,linkExtUrl_s,journalTitle_s,docType_s,domain_s"
)
_ROWS = 30
_MAX_PAGES = 200
_CRAWL_BUDGET_SECS = 25 * 60  # 25 minutes
_MIN_ABSTRACT_LEN = 100
_STRUCT_ID = "301585"


class EspciHalScienceSearchCrawler(BaseCrawler):
    site_id = "espci-hal-science-search"
    site_name = "Custom: espci-hal-science-search"
    base_url = "https://espci.hal.science"

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, params: dict) -> str | None:
        """GET via curl with retry/backoff; returns raw response text or None."""
        qs = urllib.parse.urlencode(params, doseq=True)
        full_url = f"{url}?{qs}"
        cmd = [
            "curl", "-sk", "--tls-max", "1.3",
            "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            full_url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=35,
                )
                stdout = result.stdout or b""
                text = stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                wait = 1 * (3 ** attempt)
                if attempt < 2:
                    print(f"[{self.site_id}] Empty response (attempt {attempt+1}/3), retrying in {wait}s...")
                    time.sleep(wait)
            except Exception as exc:
                wait = 1 * (3 ** attempt)
                if attempt < 2:
                    print(f"[{self.site_id}] curl error: {exc}, retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return None

    def _fetch_page(self, start: int) -> dict | None:
        """Fetch one page from the HAL Solr API. Returns parsed JSON or None."""
        params = {
            "q": "*",
            "rows": _ROWS,
            "start": start,
            "wt": "json",
            "fl": _FIELDS,
            "fq": [
                "submitType_s:file",
                "docType_s:(REPORT OR OTHER)",
                f"structId_i:{_STRUCT_ID}",
            ],
            "sort": "producedDate_s desc",
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
    def _parse_date(raw) -> str:
        """Normalise various HAL date formats to YYYY-MM-DD or YYYY."""
        if not raw:
            return ""
        text = str(raw).strip()
        # Full ISO datetime: 2023-06-14T15:20:11Z
        m = re.match(r"(\d{4}-\d{2}-\d{2})", text)
        if m:
            return m.group(1)
        # YYYY-MM
        m = re.match(r"(\d{4}-\d{2})$", text)
        if m:
            return m.group(1)
        # YYYY
        m = re.match(r"(\d{4})$", text)
        if m:
            return m.group(1)
        return text[:10] if len(text) >= 10 else text

    @staticmethod
    def _extract_filename(url: str) -> str:
        """Pull the last path segment from a URL as the original filename."""
        if not url:
            return ""
        path = urllib.parse.urlparse(url).path
        seg = path.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
        return seg if "." in seg else ""

    @staticmethod
    def _as_list(val) -> list:
        """Return val as a list regardless of whether it's a str, list, or None."""
        if val is None:
            return []
        if isinstance(val, list):
            return val
        return [val]

    @staticmethod
    def _first(val) -> str:
        """Return first element of a list-or-string, or empty string."""
        if val is None:
            return ""
        if isinstance(val, list):
            return str(val[0]) if val else ""
        return str(val)

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        limit_or_inf = str(limit) if limit is not None else "∞"
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()

        for p in range(_MAX_PAGES):
            # Wall-clock budget guard
            elapsed = time.time() - start_time
            if elapsed > _CRAWL_BUDGET_SECS:
                print(f"[{self.site_id}] 25-minute budget reached after {p} pages. Exiting.")
                break

            if limit is not None and saved >= limit:
                break

            # Progress log every 10 pages
            if p % 10 == 0 and p > 0:
                print(f"[{self.site_id}] page {p}: saved {saved}/{limit_or_inf}")

            start_offset = p * _ROWS
            data = self._fetch_page(start_offset)
            if data is None:
                print(f"[{self.site_id}] Failed to fetch page {p}. Stopping.")
                break

            response = data.get("response", {})
            docs = response.get("docs", [])

            if not docs:
                print(f"[{self.site_id}] No more documents at page {p} (start={start_offset}). Done.")
                break

            if p == 0:
                total = response.get("numFound", "?")
                print(f"[{self.site_id}] Total available on server: {total}")

            new_on_page = 0

            for doc in docs:
                if limit is not None and saved >= limit:
                    break

                try:
                    hal_id = str(doc.get("halId_s") or "")
                    docid = str(doc.get("docid") or "")
                    uri = doc.get("uri_s") or (f"https://hal.science/{hal_id}" if hal_id else "")

                    # URL-based deduplication
                    dedup_key = hal_id or uri or docid
                    if dedup_key in seen_urls:
                        continue
                    seen_urls.add(dedup_key)

                    # Title
                    title_raw = doc.get("title_s")
                    title = self._first(title_raw).strip()
                    if not title:
                        print(f"[{self.site_id}] Skipping doc {docid}: no title")
                        continue

                    # Abstract — handle list or string; skip if too short
                    abstract_raw = doc.get("abstract_s")
                    abstract = self._first(abstract_raw).strip()
                    if len(abstract) < _MIN_ABSTRACT_LEN:
                        print(
                            f"[{self.site_id}] Skipping doc {docid}: abstract too short "
                            f"({len(abstract)} chars)"
                        )
                        continue

                    # Authors
                    authors_list = self._as_list(doc.get("authFullName_s"))
                    authors = "; ".join(str(a) for a in authors_list)

                    # Publisher — first 3 structName_s entries
                    struct_list = self._as_list(doc.get("structName_s"))
                    publisher = "; ".join(str(s) for s in struct_list[:3])

                    # Keywords
                    keyword_list = self._as_list(doc.get("keyword_s"))
                    keywords = ", ".join(str(k) for k in keyword_list) if keyword_list else None

                    # Dates
                    publication_date = self._parse_date(doc.get("publicationDate_s"))
                    produced_date = self._parse_date(doc.get("producedDate_s"))

                    # Document type / category
                    doc_type = str(doc.get("docType_s") or "")

                    # Journal
                    journal_raw = doc.get("journalTitle_s")
                    journal = self._first(journal_raw)

                    # DOI
                    doi_raw = doc.get("doi_s")
                    doi = self._first(doi_raw)

                    # PDF URL — prefer files_s[0], fall back to fileMain_s
                    files_s = self._as_list(doc.get("files_s"))
                    file_main = doc.get("fileMain_s")
                    pdf_url = files_s[0] if files_s else (str(file_main) if file_main else None)
                    original_filename = self._extract_filename(pdf_url or "")

                    # Extra fields for metadata
                    domain_list = self._as_list(doc.get("domain_s"))
                    link_ext_url = doc.get("linkExtUrl_s")

                    metadata = {
                        "posted_date": str(doc.get("producedDate_s") or ""),
                        "originalFilename": original_filename,
                        "journal_raw": journal_raw,
                        "docType": doc_type,
                        "domain": domain_list,
                        "halId": hal_id,
                        "fileMain": str(file_main) if file_main else None,
                        "linkExtUrl": link_ext_url,
                        # remaining raw fields not mapped above
                        "structName_s": struct_list,
                        "files_s": files_s,
                    }

                    paper = {
                        "site_id": self.site_id,
                        "external_id": hal_id or docid,
                        "post_number": docid,
                        "title": title,
                        "abstract": abstract,
                        "published_date": publication_date,
                        "listed_date": produced_date,
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
                    print(f"[{self.site_id}] Saved {saved}/{limit_or_inf}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {doc.get('docid', '?')} failed: {exc}")
                    continue

            # If an entire page produced zero new records, pagination has looped
            if new_on_page == 0 and p > 0:
                print(
                    f"[{self.site_id}] No new records on page {p} "
                    "(all skipped/seen). Stopping."
                )
                break

            time.sleep(self._delay)

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
