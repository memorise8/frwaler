# -*- coding: utf-8 -*-
"""ANR HAL Science search crawler.

Uses the public HAL Solr API (api.archives-ouvertes.fr/search/) to collect
reports and other open-access documents submitted with attached files
from the ANR (Agence Nationale de la Recherche) HAL portal.

Starting URL reference:
  https://anr.hal.science/search/index/?q=%2A&rows=30&openAccess_bool=true&submitType_s=file&docType_s=REPORT+OR+OTHER
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

_API_URL = "https://api.archives-ouvertes.fr/search/ANR/"
_FIELDS = (
    "docid,halId_s,uri_s,title_s,abstract_s,en_abstract_s,authFullName_s,"
    "producedDate_s,submittedDate_tdate,docType_s,"
    "keyword_s,en_keyword_s,fileMain_s,files_s,doiId_s,journalTitle_s,publisher_s,"
    "instStructName_s,labStructName_s,domain_s,language_s,volume_s,issue_s,"
    "anrProjectReference_s"
)
_ROWS = 30
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_CRAWL_BUDGET_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_MIN_ABSTRACT_LEN = 100


class AnrHalScienceSearchCrawler(BaseCrawler):
    site_id = "anr-hal-science-search"
    site_name = "Custom: anr-hal-science-search"
    base_url = "https://anr.hal.science"

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
                "openAccess_bool:true",
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
        # Full ISO datetime: 2023-06-14T15:20:11Z
        m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
        if m:
            return m.group(1)
        # YYYY-MM
        m = re.match(r"(\d{4}-\d{2})$", raw)
        if m:
            return m.group(1) + "-01"
        # YYYY
        m = re.match(r"(\d{4})$", raw)
        if m:
            return m.group(1) + "-01-01"
        return raw[:10] if len(raw) >= 10 else raw

    @staticmethod
    def _extract_filename(url: str) -> str:
        """Pull the last path segment from a URL as the original filename."""
        if not url:
            return ""
        path = urllib.parse.urlparse(url).path
        seg = path.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
        seg = urllib.parse.unquote(seg)
        return seg if "." in seg else ""

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

            rows_this_page = _ROWS
            if limit is not None:
                rows_this_page = min(_ROWS, limit - saved)

            start_offset = page * _ROWS
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
                    url = doc.get("uri_s") or (f"https://hal.science/{hal_id}" if hal_id else "")

                    # URL-based deduplication
                    dedup_key = url or docid
                    if not dedup_key:
                        print(f"[{self.site_id}] Skipping doc with no url/docid")
                        continue
                    if dedup_key in seen_urls:
                        continue
                    seen_urls.add(dedup_key)

                    # Title
                    title_list = doc.get("title_s") or []
                    title = title_list[0].strip() if title_list else ""
                    if not title:
                        print(f"[{self.site_id}] Skipping doc {docid}: no title")
                        continue

                    # Abstract — prefer default, fall back to English abstract
                    abstract_list = doc.get("abstract_s") or doc.get("en_abstract_s") or []
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

                    # Keywords — prefer default, fall back to English keywords
                    keywords_list = doc.get("keyword_s") or doc.get("en_keyword_s") or []
                    keywords = ", ".join(str(k) for k in keywords_list)

                    # Dates
                    produced_date = self._parse_date(str(doc.get("producedDate_s") or ""))
                    submitted_raw = str(doc.get("submittedDate_tdate") or "")
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
                    if publisher_list:
                        publisher = "; ".join(str(p) for p in publisher_list)
                    else:
                        publisher = "; ".join(str(i) for i in inst_list[:3])

                    # Department — laboratory affiliation
                    lab_list = doc.get("labStructName_s") or []
                    department = "; ".join(str(l) for l in lab_list[:3])

                    # PDF URL — prefer direct file link from files_s, fall back to fileMain_s
                    files_s = doc.get("files_s") or []
                    pdf_url = files_s[0] if files_s else str(doc.get("fileMain_s") or "")
                    original_filename = self._extract_filename(pdf_url)

                    # Extra metadata
                    domain_list = doc.get("domain_s") or []
                    lang = str(doc.get("language_s") or "")
                    volume = str(doc.get("volume_s") or "")
                    issue = str(doc.get("issue_s") or "")
                    anr_ref = doc.get("anrProjectReference_s") or []

                    metadata = {
                        "posted_date": submitted_raw,
                        "originalFilename": original_filename,
                        "halId": hal_id,
                        "docType": doc_type,
                        "domains": domain_list,
                        "labStructName": [str(l) for l in lab_list],
                        "instStructName": [str(i) for i in inst_list],
                        "language": lang,
                        "files_s": files_s,
                        "fileMain_s": doc.get("fileMain_s"),
                        "journal_raw": journal,
                        "series": None,
                        "volume": volume,
                        "issue": issue,
                        "anrProjectReference": anr_ref,
                    }

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
