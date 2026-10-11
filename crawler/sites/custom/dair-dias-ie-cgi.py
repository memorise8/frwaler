# -*- coding: utf-8 -*-
"""Crawler for DIAS Access to Institutional Repository (dair.dias.ie).

Uses the EPrints JSON export endpoint (export_dias_JSON.js) filtered to
type=article (matching the advanced-search starting URL). The endpoint
returns the full matching result set with available abstracts and metadata;
no detail-page fetches are needed. A durable position and export digest allow
interrupted work to resume without skipping changed or failed records.

PDF URL pattern: {base_url}/{eprintid}/{pos}/{filename}
"""

import hashlib
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
        """Save the complete article export with a durable position checkpoint."""
        self._exhausted = False
        self._source_failure = None
        if limit is not None and limit <= 0:
            return 0
        self._check_cancelled()
        cursor = dict(self.delivery_cursor or {})
        scope = cursor.get("scope", "articles")
        index = cursor.get("next_index", 0)
        total = cursor.get("total", 0)
        expected_digest = cursor.get("export_digest", "")
        if (type(cursor.get("version", 1)) is not int or cursor.get("version", 1) != 1
                or scope not in ("articles", "done")
                or type(index) is not int or type(total) is not int
                or not 0 <= index <= total
                or not isinstance(expected_digest, str)
                or (cursor and (total <= 0 or not re.fullmatch(r"[a-f0-9]{64}", expected_digest)))
                or (scope == "done" and index != total)):
            raise ValueError("invalid DAIR export cursor")
        if scope == "done":
            self._mark_exhausted()
            return 0
        started = time.monotonic()
        budget = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
        if time.monotonic() - started >= budget:
            return 0
        saved = 0
        try:
            records = self._fetch_all_records()
            self._check_cancelled()
            if not isinstance(records, list) or not records:
                self._report_source_failure("fetch_failed")
                raise ValueError("missing or empty DAIR article export")
            ids = []
            for record in records:
                if (not isinstance(record, dict)
                        or type(record.get("eprintid")) is not int or record["eprintid"] <= 0
                        or record.get("type") != "article"
                        or record.get("eprint_status") != "archive"
                        or record.get("metadata_visibility") != "show"):
                    raise ValueError("invalid DAIR article identity or export scope")
                ids.append(record["eprintid"])
            if len(set(ids)) != len(ids):
                raise ValueError("duplicate DAIR article identity in export")
            digest = hashlib.sha256(json.dumps(records, sort_keys=True,
                                               separators=(",", ":")).encode()).hexdigest()
            if expected_digest and (expected_digest != digest or total != len(records)):
                raise ValueError("DAIR export changed since checkpoint; overlap review required")
            total = len(records)
            for offset in range(index, total):
                self._check_cancelled()
                if ((limit is not None and saved >= limit)
                        or time.monotonic() - started >= budget):
                    return saved
                paper = self._record_to_paper(records[offset])
                if paper is None:
                    self._report_source_failure("item_failed")
                    raise ValueError("DAIR article could not be parsed")
                self._check_cancelled()
                self._save_paper(paper)
                if self._last_save_created:
                    saved += 1
                self._advance_cursor(dict(version=1, scope="articles", next_index=offset + 1,
                                          total=total, export_digest=digest), items_done=1)
            self._check_cancelled()
            self._advance_cursor(dict(version=1, scope="done", next_index=total,
                                      total=total, export_digest=digest))
            self._mark_exhausted()
            return saved
        except Exception:
            self._report_source_failure("invalid_response")
            raise
