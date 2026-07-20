# -*- coding: utf-8 -*-
"""Crawler for IMT Lucca EPrints repository (eprints.imtlucca.it/cgi/search).

Uses the EPrints JSON export endpoint with search_offset pagination.
Full abstracts are included in the export — no detail-page fetches needed.
PDF URL pattern: {base_url}/{eprintid}/1/{filename}
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

# EPrints search expression: article + monograph + thesis, published, archive
_EXP = (
    "0|1|-date/creators_name/title|archive|-"
    "|ispublished:ispublished:ANY:EQ:pub"
    "|type:type:ANY:EQ:article monograph thesis"
    "|-"
    "|eprint_status:eprint_status:ANY:EQ:archive"
    "|metadata_visibility:metadata_visibility:ANY:EQ:show"
)
_EXP_ENCODED = urllib.parse.quote(_EXP, safe="")

_PAGE_SIZE = 20
_SAFETY_CAP_PAGES = 200
_BUDGET_SECS = 25 * 60  # 25 minutes


class EprintsImtLuccaItCgiCrawler(BaseCrawler):
    """Crawler for IMT Lucca institutional repository (EPrints 3.3.16)."""

    site_id = "eprints-imtlucca-it-cgi"
    site_name = "Custom: eprints-imtlucca-it-cgi"
    base_url = "http://eprints.imtlucca.it"

    _EXPORT_BASE = (
        "http://eprints.imtlucca.it"
        "/cgi/search/archive/advanced/export_eprints_JSON.js"
    )

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """GET via curl with exponential-backoff retry (1s / 3s / 9s)."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=35
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

    def _fetch_page(self, offset: int, page_size: int) -> list | None:
        """Fetch one page from the JSON export endpoint."""
        url = (
            f"{self._EXPORT_BASE}"
            f"?screen=Search&dataset=archive&_action_export=1&output=JSON"
            f"&exp={_EXP_ENCODED}"
            f"&n={page_size}&search_offset={offset}&cache="
        )
        raw = self._curl_get(url)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return data
            print(f"[{self.site_id}] unexpected JSON type at offset {offset}: {type(data)}")
            return None
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] JSON decode error at offset {offset}: {exc}")
            return None

    # ------------------------------------------------------------------
    # Field parsers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_date(raw) -> str:
        """Normalise EPrints date (int year, 'YYYY-MM', 'YYYY-MM-DD') → string."""
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
    def _parse_listed_date(datestamp: str) -> str:
        """'2025-04-03 08:36:33' → '2025-04-03'."""
        if not datestamp:
            return ""
        return datestamp[:10]

    @staticmethod
    def _build_authors(creators: list) -> str:
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
    def _build_pdf_url(base_url: str, eprintid: int,
                       documents: list) -> tuple:
        """Return (pdf_url, original_filename) from documents list.

        URL pattern confirmed from detail-page meta: {base}/{id}/1/{file}.
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
            pdf_url = f"{base_url}/{eprintid}/1/{urllib.parse.quote(main)}"
            return pdf_url, main
        return None, None

    @staticmethod
    def _build_keywords(rec: dict) -> str:
        """Prefer the 'keywords' field; fall back to subject codes."""
        kw_raw = (rec.get("keywords") or "").strip()
        if kw_raw:
            # Strip common lead-in labels
            kw_raw = re.sub(r"(?i)^keywords?\s*[:\-]\s*", "", kw_raw).strip()
            # Truncate at JEL / classification lines
            kw_raw = re.split(r"\n\s*(?:JEL|Keywords|Classification|Codes?)",
                               kw_raw, maxsplit=1)[0].strip()
            # Normalise semicolons → commas
            return kw_raw.replace(";", ",").strip(", ")
        subjects = rec.get("subjects") or []
        return ", ".join(subjects)

    # ------------------------------------------------------------------
    # Record conversion
    # ------------------------------------------------------------------

    def _record_to_paper(self, rec: dict) -> dict | None:
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

        # publisher: prefer explicit 'publisher', fall back to 'institution'
        publisher = (
            (rec.get("publisher") or rec.get("institution") or "").strip()
        )

        # journal / publication name
        journal = (rec.get("publication") or rec.get("journal") or "").strip()

        keywords = self._build_keywords(rec)
        doi = (rec.get("doi") or "").strip()
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
            "eprintid": eprintid,
            "type": rec.get("type"),
            "monograph_type": rec.get("monograph_type") or None,
            "place_of_pub": rec.get("place_of_pub") or None,
            "full_text_status": rec.get("full_text_status") or None,
            "subjects": rec.get("subjects") or None,
            "divisions": rec.get("divisions") or None,
            "date_type": rec.get("date_type") or None,
            "eprint_status": rec.get("eprint_status") or None,
        }
        # Drop None values to keep metadata compact
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
        """Crawl IMT Lucca EPrints via JSON export with offset pagination.

        Parameters
        ----------
        limit:
            Maximum records to save. None = unlimited.
        """
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        limit_or_inf = limit if limit is not None else float("inf")
        page_num = 0   # 0-based page counter (for logging / safety cap)
        offset = 0     # item offset into search results

        while True:
            try:
                # --- time budget ---
                elapsed = time.time() - start_time
                if elapsed >= _BUDGET_SECS:
                    print(
                        f"[{self.site_id}] 25-minute budget reached. "
                        f"Stopping cleanly."
                    )
                    break

                # --- limit ---
                if saved >= limit_or_inf:
                    break

                # --- safety cap ---
                if page_num >= _SAFETY_CAP_PAGES:
                    print(
                        f"[{self.site_id}] Safety cap of "
                        f"{_SAFETY_CAP_PAGES} pages reached. Stopping."
                    )
                    break

                # --- progress log every 10 pages ---
                if page_num > 0 and page_num % 10 == 0:
                    limit_str = str(limit) if limit is not None else "∞"
                    print(
                        f"[{self.site_id}] page {page_num}: "
                        f"saved {saved}/{limit_str}"
                    )

                # --- page size (respect limit) ---
                if limit is not None:
                    page_size = min(_PAGE_SIZE, limit - saved)
                else:
                    page_size = _PAGE_SIZE

                if page_size <= 0:
                    break

                # --- fetch ---
                records = self._fetch_page(offset, page_size)

                if records is None:
                    print(
                        f"[{self.site_id}] fetch failed at offset {offset}. "
                        f"Stopping."
                    )
                    break

                if not records:
                    print(
                        f"[{self.site_id}] no records at offset {offset}. "
                        f"Done."
                    )
                    break

                # --- process records ---
                new_on_page = 0
                for rec in records:
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
                        print(
                            f"[{self.site_id}] item eprintid={eprintid} "
                            f"parse failed: {exc}"
                        )
                        continue

                    if paper is None:
                        continue

                    try:
                        self._save_paper(paper)
                        saved += 1
                        limit_str = str(limit) if limit is not None else "∞"
                        print(
                            f"[{self.site_id}] saved {saved}/{limit_str}: "
                            f"{paper['title'][:60]}"
                        )
                    except Exception as exc:
                        print(
                            f"[{self.site_id}] save failed for "
                            f"eprintid={eprintid}: {exc}"
                        )
                        continue

                    time.sleep(0.2)  # gentle rate limit; no detail fetches

                # --- pagination termination conditions ---
                if new_on_page == 0 and records:
                    print(
                        f"[{self.site_id}] all records on page {page_num} "
                        f"already seen (loop detected). Stopping."
                    )
                    break

                if len(records) < page_size:
                    # Partial page → last page
                    print(
                        f"[{self.site_id}] partial page "
                        f"({len(records)} < {page_size}). Done."
                    )
                    break

                offset += len(records)
                page_num += 1

            except KeyboardInterrupt:
                print(f"[{self.site_id}] interrupted by user.")
                raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
