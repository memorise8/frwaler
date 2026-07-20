# -*- coding: utf-8 -*-
"""Crawler for the Drugs and Alcohol (Ireland) EPrints repository
(drugsandalcohol.ie/cgi/search).

Uses the EPrints JSON export endpoint (``export_ndc_JSON.js``) with
``search_offset``/``n`` pagination — much faster and more robust than
scraping the HTML result list, and it already carries the full abstract,
creators, subjects and document metadata.

Plain ``requests``/``curl`` traffic passes straight through the site's
bot-check page (a JS drag-to-order challenge) — it appears to be triggered
by TLS/HTTP client fingerprinting rather than user-agent string, and a
plain GET with a normal browser User-Agent header reaches real content
directly (verified against both the search and detail pages during the
2026-07-18 probe). No Playwright/browser rendering is required.
"""

import html as html_module
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

# EPrints search expression: type=monograph, published, archive, visible.
# Matches the starting URL's filters exactly.
_EXP = (
    "0|1|-date/browse_by/title|archive|-"
    "|type:type:ANY:EQ:monograph"
    "|-"
    "|eprint_status:eprint_status:ANY:EQ:archive"
    "|metadata_visibility:metadata_visibility:ANY:EQ:show"
)
_EXP_ENCODED = urllib.parse.quote(_EXP, safe="")

_PAGE_SIZE = 50
_SAFETY_CAP_PAGES = 200
_BUDGET_SECS = 25 * 60  # 25 minutes
_MIN_ABSTRACT_LEN = 100
_DOI_RE = re.compile(r"10\.\d{4,9}/\S+", re.I)


class DrugsandalcoholIeCgiCrawler(BaseCrawler):
    """Crawler for the Drugs and Alcohol (Ireland) EPrints repository."""

    site_id = "drugsandalcohol-ie-cgi"
    site_name = "Custom: drugsandalcohol-ie-cgi"
    base_url = "https://www.drugsandalcohol.ie"

    _EXPORT_BASE = (
        "https://www.drugsandalcohol.ie"
        "/cgi/search/archive/type/export_ndc_JSON.js"
    )

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """GET via curl with exponential-backoff retry (1s / 3s / 9s)."""
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", "40",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: application/json,text/html,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=45
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
            f"?dataset=archive&screen=Search&_action_export=1&output=JSON"
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
    def _strip_html(raw_html: str) -> str:
        """Strip HTML tags from ``body_html``, tolerating malformed markup."""
        if not raw_html:
            return ""
        text = html_module.unescape(raw_html)
        soup = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(text, parser)
                break
            except Exception:
                continue
        if soup is None:
            # Last-resort: crude tag stripping so a malformed fragment
            # never crashes the run.
            return re.sub(r"<[^>]+>", " ", text).strip()
        return soup.get_text(separator=" ", strip=True)

    @staticmethod
    def _parse_date(raw) -> str:
        """Normalise EPrints date ('YYYY', 'YYYY-MM', 'YYYY-MM-DD') → string."""
        if raw is None:
            return ""
        return str(raw).strip()

    @staticmethod
    def _parse_listed_date(datestamp: str) -> str:
        """'2026-07-16 08:01:33' -> '2026-07-16'."""
        if not datestamp:
            return ""
        return str(datestamp)[:10]

    @staticmethod
    def _build_authors(rec: dict) -> str:
        """Prefer personal creators; fall back to corporate creators."""
        parts = []
        for c in rec.get("creators") or []:
            name = c.get("name") or {}
            given = (name.get("given") or "").strip()
            family = (name.get("family") or "").strip()
            full = " ".join(filter(None, [given, family]))
            if full:
                parts.append(full)
        if parts:
            return "; ".join(parts)
        corp = [c.strip() for c in (rec.get("corp_creators") or []) if c and c.strip()]
        return "; ".join(corp)

    @staticmethod
    def _build_pdf_url(base_url: str, eprintid: int, documents: list) -> tuple:
        """Return (pdf_url, original_filename) from the documents list."""
        for doc in documents or []:
            if doc.get("security") != "public":
                continue
            mime = (doc.get("mime_type") or doc.get("format") or "").lower()
            if "pdf" not in mime:
                continue
            main = (doc.get("main") or "").strip()
            if not main:
                continue
            pos = doc.get("pos") or doc.get("placement") or 1
            pdf_url = f"{base_url}/{eprintid}/{pos}/{urllib.parse.quote(main)}"
            return pdf_url, main
        return None, None

    @staticmethod
    def _build_keywords(rec: dict) -> str:
        """Prefer explicit keywords; fall back to subject-hierarchy words."""
        kw_raw = (rec.get("keywords") or "").strip()
        if kw_raw:
            return kw_raw.replace(";", ",").strip(", ")
        words = (rec.get("vol_subject_list_words_last") or "").strip()
        if words:
            parts = [p.strip() for p in words.split("--") if p.strip()]
            return ", ".join(parts)
        return ""

    @staticmethod
    def _extract_doi(rec: dict) -> str:
        for key in ("id_number", "issn"):
            val = rec.get(key) or ""
            m = _DOI_RE.search(str(val))
            if m:
                return m.group(0).rstrip(".")
        return ""

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
        if len(abstract) < _MIN_ABSTRACT_LEN:
            fallback = self._strip_html(rec.get("body_html") or "")
            if len(fallback) >= len(abstract):
                abstract = fallback

        if len(abstract) < _MIN_ABSTRACT_LEN:
            print(
                f"[{self.site_id}] skip eprintid={eprintid}: "
                f"abstract too short ({len(abstract)} chars)"
            )
            return None

        authors = self._build_authors(rec)
        published_date = self._parse_date(rec.get("date"))
        listed_date = self._parse_listed_date(rec.get("datestamp") or "")

        pdf_url, original_filename = self._build_pdf_url(
            self.base_url, eprintid, rec.get("documents") or []
        )

        publisher = (rec.get("publisher") or "").strip()
        if not publisher:
            corp = [c.strip() for c in (rec.get("corp_creators") or []) if c and c.strip()]
            publisher = "; ".join(corp)

        institution = (rec.get("institution") or "").strip()
        department = institution if institution and institution != publisher else ""

        journal = (rec.get("publication") or "").strip()
        keywords = self._build_keywords(rec)
        doi = self._extract_doi(rec)
        category = (rec.get("monograph_type") or rec.get("type") or "").strip()

        detail_url = f"{self.base_url}/{eprintid}/"

        metadata = {
            "posted_date": rec.get("datestamp"),
            "originalFilename": original_filename,
            "journal_raw": rec.get("publication") or None,
            "series": rec.get("series") or None,
            "volume": rec.get("volume") or None,
            "issue": rec.get("number") or None,
            "eprintid": eprintid,
            "type": rec.get("type"),
            "monograph_type": rec.get("monograph_type") or None,
            "publication_type": rec.get("publication_type") or None,
            "place_of_pub": rec.get("place_of_pub") or None,
            "pages": rec.get("pages") or None,
            "full_text_status": rec.get("full_text_status") or None,
            "subjects": rec.get("subjects") or None,
            "id_number": rec.get("id_number") or None,
            "issn": rec.get("issn") or None,
            "institution": institution or None,
            "drug_type": rec.get("drug_type") or None,
            "intervention_type": rec.get("intervention_type") or None,
            "related_url": rec.get("related_url") or None,
            "official_url": rec.get("official_url") or None,
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
        """Crawl drugsandalcohol.ie via the EPrints JSON export endpoint.

        Parameters
        ----------
        limit:
            Maximum records to save. ``None`` means unlimited.
        """
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        limit_or_inf = limit if limit is not None else float("inf")
        page_num = 0
        offset = 0

        while True:
            try:
                elapsed = time.time() - start_time
                if elapsed >= _BUDGET_SECS:
                    print(f"[{self.site_id}] 25-minute budget reached. Stopping cleanly.")
                    break

                if saved >= limit_or_inf:
                    break

                if page_num >= _SAFETY_CAP_PAGES:
                    print(f"[{self.site_id}] Safety cap of {_SAFETY_CAP_PAGES} pages reached. Stopping.")
                    break

                if page_num > 0 and page_num % 10 == 0:
                    limit_str = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] page {page_num}: saved {saved}/{limit_str}")

                if page_num > 0:
                    time.sleep(1.0)  # gentle rate limit between page fetches
                records = self._fetch_page(offset, _PAGE_SIZE)

                if records is None:
                    print(f"[{self.site_id}] fetch failed at offset {offset}. Stopping.")
                    break

                if not records:
                    print(f"[{self.site_id}] no records at offset {offset}. Done.")
                    break

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
                        print(f"[{self.site_id}] item eprintid={eprintid} failed: {exc}")
                        continue

                    if paper is None:
                        continue

                    try:
                        self._save_paper(paper)
                        saved += 1
                        limit_str = str(limit) if limit is not None else "inf"
                        print(f"[{self.site_id}] saved {saved}/{limit_str}: {paper['title'][:60]}")
                    except Exception as exc:
                        print(f"[{self.site_id}] save failed for eprintid={eprintid}: {exc}")
                        continue

                if new_on_page == 0 and records:
                    print(f"[{self.site_id}] all records on page {page_num} already seen (loop detected). Stopping.")
                    break

                if len(records) < _PAGE_SIZE:
                    print(f"[{self.site_id}] partial page ({len(records)} < {_PAGE_SIZE}). Done.")
                    break

                offset += len(records)
                page_num += 1

            except KeyboardInterrupt:
                print(f"[{self.site_id}] interrupted by user.")
                raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
