# -*- coding: utf-8 -*-
"""Crawler for DIAS Access to Institutional Repository (dair.dias.ie).

Uses the EPrints JSON export endpoint (export_dias_JSON.js) filtered to
type=article (matching the advanced-search starting URL). The endpoint
returns the full matching result set in a single response with complete
abstracts and metadata — no detail-page fetches are needed. Pagination is
therefore performed in-memory over the fetched list, in page-size chunks,
to satisfy limit/logging/safety-cap semantics.

PDF URL pattern: {base_url}/{eprintid}/{pos}/{filename}
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

# absolute import — spec_from_file_location has no package context
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)
))))
from crawler.base_crawler import BaseCrawler  # noqa: E402

# EPrints search expression, matching the advanced-search starting URL:
# dataset=archive, type=article, eprint_status=archive, metadata_visibility=show
_EXP = (
    "0|1|-date/creators_name/title|archive|-"
    "|type:type:ANY:EQ:article"
    "|-"
    "|eprint_status:eprint_status:ANY:EQ:archive"
    "|metadata_visibility:metadata_visibility:ANY:EQ:show"
)
_EXP_ENCODED = urllib.parse.quote(_EXP, safe="")

_PAGE_SIZE = 20
_SAFETY_CAP_PAGES = 200
_BUDGET_SECS = 25 * 60  # 25 minutes


class DairDiasIeCgiCrawler(BaseCrawler):
    """Crawler for the DIAS institutional repository (EPrints 3.4.3)."""

    site_id = "dair-dias-ie-cgi"
    site_name = "Custom: dair-dias-ie-cgi"
    base_url = "https://dair.dias.ie"

    _EXPORT_URL = (
        "https://dair.dias.ie"
        "/cgi/search/archive/advanced/export_dias_JSON.js"
        f"?screen=Search&dataset=archive&_action_export=1&output=JSON"
        f"&exp={_EXP_ENCODED}&n="
    )

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url):
        """GET via curl with exponential-backoff retry (1s / 3s / 9s)."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "60",
            "-H", f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=65
                )
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
            except Exception as exc:
                print(f"[{self.site_id}] curl error attempt {attempt + 1}/3: {exc}")
            if attempt < 2:
                wait = [1, 3, 9][attempt]
                print(f"[{self.site_id}] retrying in {wait}s...")
                time.sleep(wait)
        return None

    def _fetch_all_records(self):
        """Fetch the full matching result set (single bulk JSON export)."""
        raw = self._curl_get(self._EXPORT_URL)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] JSON decode error: {exc}")
            return None
        if not isinstance(data, list):
            print(f"[{self.site_id}] unexpected JSON type: {type(data)}")
            return None
        return data

    # ------------------------------------------------------------------
    # Field parsers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_date(raw):
        """Normalise EPrints date (int year, 'YYYY-MM', 'YYYY-MM-DD') -> string."""
        if raw is None:
            return ""
        s = str(raw).strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
            return s
        if re.match(r"^\d{4}-\d{2}$", s):
            return s
        if re.match(r"^\d{4}$", s):
            return s
        return s

    @staticmethod
    def _parse_listed_date(datestamp):
        """'2025-01-14 14:08:50' -> '2025-01-14'."""
        if not datestamp:
            return ""
        return str(datestamp)[:10]

    @staticmethod
    def _build_authors(creators):
        """Return 'Given Family; Given Family; ...' string."""
        parts = []
        for c in creators or []:
            name = c.get("name") or {}
            given = (name.get("given") or "").strip()
            family = (name.get("family") or "").strip()
            full = " ".join(filter(None, [given, family]))
            if full:
                parts.append(full)
        return "; ".join(parts)

    @staticmethod
    def _build_pdf_url(base_url, eprintid, documents):
        """Return (pdf_url, original_filename) from the documents list.

        URL pattern confirmed against the REST record: {base}/{id}/{pos}/{file}.
        Prefers the first public PDF document, skipping thumbnails/coversheets.
        """
        for doc in documents or []:
            if doc.get("security") != "public":
                continue
            mime = (doc.get("mime_type") or doc.get("format") or "").lower()
            if "pdf" not in mime:
                continue
            main = (doc.get("main") or "").strip()
            if not main:
                continue
            pos = doc.get("placement") or doc.get("pos") or 1
            pdf_url = f"{base_url}/{eprintid}/{pos}/{urllib.parse.quote(main)}"
            return pdf_url, main
        return None, None

    @staticmethod
    def _build_keywords(rec):
        """Prefer the 'keywords' field; fall back to subject/division codes."""
        kw_raw = (rec.get("keywords") or "").strip()
        if kw_raw:
            kw_raw = re.sub(r"(?i)^keywords?\s*[:\-]\s*", "", kw_raw).strip()
            return kw_raw.replace(";", ",").strip(", ")
        subjects = rec.get("subjects") or []
        return ", ".join(subjects)

    # ------------------------------------------------------------------
    # Record conversion
    # ------------------------------------------------------------------

    def _record_to_paper(self, rec):
        """Convert one EPrints JSON record to a paper dict for _save_paper."""
        eprintid = rec.get("eprintid")
        if not eprintid:
            return None

        title = (rec.get("title") or "").strip()
        if not title:
            return None

        abstract = (rec.get("abstract") or "").strip()
        if len(abstract) < 50:
            print(
                f"[{self.site_id}] skip eprintid={eprintid}: "
                f"abstract too short ({len(abstract)} chars)"
            )
            return None

        authors = self._build_authors(rec.get("creators") or [])
        published_date = self._parse_date(rec.get("date"))
        listed_date = self._parse_listed_date(rec.get("datestamp") or "")

        pdf_url, original_filename = self._build_pdf_url(
            self.base_url, eprintid, rec.get("documents") or []
        )

        publisher = (rec.get("publisher") or "").strip()
        journal = (rec.get("publication") or "").strip()
        keywords = self._build_keywords(rec)
        doi_raw = (rec.get("id_number") or "").strip()
        doi = doi_raw if "doi.org" in doi_raw.lower() or doi_raw.lower().startswith("10.") else doi_raw
        department = "; ".join(rec.get("divisions") or [])
        category = rec.get("type") or ""

        detail_url = f"{self.base_url}/{eprintid}/"

        metadata = {
            "posted_date": rec.get("datestamp"),
            "originalFilename": original_filename,
            "journal_raw": rec.get("publication") or None,
            "series": rec.get("series") or None,
            "volume": rec.get("volume") or None,
            "issue": rec.get("number") or None,
            "issn": rec.get("issn") or None,
            "online_issn": rec.get("online_issn") or None,
            "eprintid": eprintid,
            "type": rec.get("type"),
            "monograph_type": rec.get("monograph_type") or None,
            "full_text_status": rec.get("full_text_status") or None,
            "subjects": rec.get("subjects") or None,
            "divisions": rec.get("divisions") or None,
            "date_type": rec.get("date_type") or None,
            "eprint_status": rec.get("eprint_status") or None,
            "refereed": rec.get("refereed") or None,
            "official_url": rec.get("official_url") or None,
            "pagerange": rec.get("pagerange") or None,
        }
        metadata = {k: v for k, v in metadata.items() if v is not None}

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": str(eprintid),
            "post_number": str(eprintid),
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "url": detail_url,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "doi": doi,
            "publisher": publisher,
            "department": department,
            "journal": journal,
            "keywords": keywords,
            "category": category,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl the DIAS repository via the JSON bulk export.

        The export endpoint returns the full type=article result set in
        one response; pagination below walks that list in fixed-size
        chunks so limit/logging/safety-cap/dedup semantics match a normal
        paginated crawl.

        Parameters
        ----------
        limit:
            Maximum records to save. None = unlimited.
        """
        saved = 0
        seen_urls = set()
        start_time = time.time()
        limit_or_inf = limit if limit is not None else float("inf")

        print(f"[{self.site_id}] fetching bulk export...")
        all_records = self._fetch_all_records()
        if not all_records:
            print(f"[{self.site_id}] no records returned from export. Done.")
            return 0
        print(f"[{self.site_id}] export returned {len(all_records)} candidate records")

        page_num = 0  # 0-based page counter (for logging / safety cap)
        offset = 0

        while True:
            try:
                # --- time budget ---
                elapsed = time.time() - start_time
                if elapsed >= _BUDGET_SECS:
                    print(f"[{self.site_id}] 25-minute budget reached. Stopping cleanly.")
                    break

                # --- limit ---
                if saved >= limit_or_inf:
                    break

                # --- safety cap ---
                if page_num >= _SAFETY_CAP_PAGES:
                    print(f"[{self.site_id}] Safety cap of {_SAFETY_CAP_PAGES} pages reached. Stopping.")
                    break

                # --- progress log every 10 pages ---
                if page_num > 0 and page_num % 10 == 0:
                    limit_str = str(limit) if limit is not None else "∞"
                    print(f"[{self.site_id}] page {page_num}: saved {saved}/{limit_str}")

                # --- slice the current page out of the bulk result ---
                page_records = all_records[offset:offset + _PAGE_SIZE]
                if not page_records:
                    print(f"[{self.site_id}] no records at offset {offset}. Done.")
                    break

                # --- process records ---
                new_on_page = 0
                for rec in page_records:
                    if saved >= limit_or_inf:
                        break

                    eprintid = rec.get("eprintid")
                    url_key = f"{self.base_url}/{eprintid}/"

                    if url_key in seen_urls:
                        continue
                    seen_urls.add(url_key)
                    new_on_page += 1

                    try:
                        paper = self._record_to_paper(rec)
                    except Exception as exc:
                        print(f"[{self.site_id}] item eprintid={eprintid} parse failed: {exc}")
                        continue

                    if paper is None:
                        continue

                    try:
                        self._save_paper(paper)
                        saved += 1
                        limit_str = str(limit) if limit is not None else "∞"
                        print(f"[{self.site_id}] saved {saved}/{limit_str}: {paper['title'][:60]}")
                    except Exception as exc:
                        print(f"[{self.site_id}] save failed for eprintid={eprintid}: {exc}")
                        continue

                    time.sleep(0.2)  # gentle pacing; no per-item network fetch

                # --- pagination termination conditions ---
                if new_on_page == 0 and page_records:
                    print(f"[{self.site_id}] all records on page {page_num} already seen (loop detected). Stopping.")
                    break

                if len(page_records) < _PAGE_SIZE:
                    print(f"[{self.site_id}] partial page ({len(page_records)} < {_PAGE_SIZE}). Done.")
                    break

                offset += len(page_records)
                page_num += 1

            except KeyboardInterrupt:
                print(f"[{self.site_id}] interrupted by user.")
                raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
