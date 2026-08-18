# -*- coding: utf-8 -*-
"""Crawler for CERN CDS Published Articles.

Enumeration  : INSPIRE HEP REST API (inspirehep.net/api/literature) filtered
               for records that carry a CDS external system identifier.
               Returns ~110K records — the CERN Published Articles collection.
Detail fetch : CDS record HTML page (cds.cern.ch/record/<id>) for PDF URL,
               original filename, and online-listing date.

Starting URL : https://cds.cern.ch/collection/Published%20Articles?ln=en
               (The collection browse page only shows the 13 most recent
               additions and cannot be paginated; the INSPIRE API is used as
               the reliable enumeration backend.)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class CdsCernChCollectionCrawler(BaseCrawler):
    site_id = "cds-cern-ch-collection"
    site_name = "Custom: cds-cern-ch-collection"
    base_url = "https://cds.cern.ch"
    DELIVERY_ORDER = "newest_first"

    _INSPIRE_API = "https://inspirehep.net/api/literature"
    # All INSPIRE literature records that carry a CDS external identifier are
    # exactly the CDS "Published Articles" collection.
    _INSPIRE_QUERY = "external_system_identifiers.schema:CDS"
    _INSPIRE_FIELDS = ",".join([
        "external_system_identifiers",
        "dois",
        "titles",
        "authors",
        "abstracts",
        "keywords",
        "publication_info",
        "earliest_date",
        "arxiv_eprints",
    ])
    _PAGE_SIZE = 25
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes hard stop
    _MIN_ABSTRACT_CHARS = 50

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url: str, *, timeout: int = 60) -> str | None:
        """GET *url* via curl with up to 3 retries and exponential back-off."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: application/json,text/html,*/*;q=0.8",
            url,
        ]
        waits = [1, 3, 9]
        last_error = "unknown"
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 10,
                    check=False,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} {stderr}"
            except Exception as exc:
                last_error = str(exc)
            if attempt < 2:
                print(
                    f"[{self.site_id}] curl retry {attempt + 1}/3 for {url}: "
                    f"{last_error}; sleeping {waits[attempt]}s"
                )
                time.sleep(waits[attempt])
        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    def _fetch_json(self, url: str, *, timeout: int = 60) -> dict | None:
        raw = self._curl(url, timeout=timeout)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] JSON decode failed for {url}: {exc}")
            return None

    def _parse_html(self, raw: str) -> BeautifulSoup | None:
        """Try BeautifulSoup parsers in order; return None if all fail."""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------
    # INSPIRE HEP API helpers
    # ------------------------------------------------------------------

    def _fetch_inspire_page(self, page: int) -> dict | None:
        params = urlencode({
            "sort": "mostrecent",
            "size": self._PAGE_SIZE,
            "page": page,
            "fields": self._INSPIRE_FIELDS,
            "q": self._INSPIRE_QUERY,
        })
        url = f"{self._INSPIRE_API}?{params}"
        return self._fetch_json(url, timeout=60)

    @staticmethod
    def _get_cds_id(meta: dict) -> str | None:
        for esi in (meta.get("external_system_identifiers") or []):
            if esi.get("schema") == "CDS":
                val = (esi.get("value") or "").strip()
                if val:
                    return val
        return None

    @staticmethod
    def _get_title(meta: dict) -> str:
        for t in (meta.get("titles") or []):
            val = (t.get("title") or "").strip()
            if val:
                return val
        return ""

    @staticmethod
    def _get_abstract(meta: dict) -> str:
        for a in (meta.get("abstracts") or []):
            val = (a.get("value") or "").strip()
            if val:
                return val
        return ""

    @staticmethod
    def _get_authors(meta: dict) -> str:
        names = []
        for a in (meta.get("authors") or []):
            name = (a.get("full_name") or "").strip()
            if name:
                names.append(name)
        return "; ".join(names)

    @staticmethod
    def _get_doi(meta: dict) -> str | None:
        for d in (meta.get("dois") or []):
            val = (d.get("value") or "").strip()
            if val:
                return val
        return None

    @staticmethod
    def _get_pub_info(meta: dict) -> tuple[str, str, str, str]:
        """Return (journal, volume, issue, year_str)."""
        for pub in (meta.get("publication_info") or []):
            j = (pub.get("journal_title") or "").strip()
            if j:
                return (
                    j,
                    str(pub.get("journal_volume", "") or ""),
                    str(pub.get("journal_issue", "") or ""),
                    str(pub.get("year", "") or ""),
                )
        return "", "", "", ""

    @staticmethod
    def _get_keywords(meta: dict) -> str:
        vals = []
        for k in (meta.get("keywords") or []):
            val = (k.get("value") or "").strip()
            if val and val not in vals:
                vals.append(val)
        return ", ".join(vals[:20])

    @staticmethod
    def _normalize_date(s: object) -> str | None:
        """Normalize various date strings to ISO YYYY-MM-DD (best-effort)."""
        if not s:
            return None
        s = str(s).strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
            return s
        if re.match(r"^\d{4}-\d{2}$", s):
            return s + "-01"
        if re.match(r"^\d{4}$", s):
            return s + "-01-01"
        m = re.match(r"^(\d{4})[/\-](\d{2})[/\-](\d{2})$", s)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        return s or None

    # ------------------------------------------------------------------
    # CDS detail page
    # ------------------------------------------------------------------

    def _fetch_cds_detail(
        self, cds_id: str
    ) -> tuple[str | None, str | None, str | None]:
        """Fetch CDS record HTML; return (pdf_url, original_filename, listed_date)."""
        url = f"{self.base_url}/record/{cds_id}"
        raw = self._curl(url, timeout=30)
        if not raw:
            return None, None, None

        soup = self._parse_html(raw)
        if soup is None:
            return None, None, None

        pdf_url: str | None = None
        original_filename: str | None = None
        listed_date: str | None = None

        # <meta name="citation_pdf_url" content="...">
        tag_pdf = soup.find("meta", attrs={"name": "citation_pdf_url"})
        if tag_pdf and tag_pdf.get("content"):
            pdf_url = tag_pdf["content"].strip()
            parts = [p for p in pdf_url.split("/") if p]
            if parts:
                original_filename = parts[-1]

        # <meta name="citation_online_date" content="YYYY/MM/DD">
        tag_date = soup.find("meta", attrs={"name": "citation_online_date"})
        if tag_date and tag_date.get("content"):
            raw_date = tag_date["content"].strip().replace("/", "-")
            listed_date = self._normalize_date(raw_date)

        return pdf_url, original_filename, listed_date

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.monotonic()
        limit_str = str(limit) if limit is not None else "∞"
        saved = 0
        page = (self.delivery_cursor or {}).get("page", 1)
        total_records: int | None = None
        seen_urls: set[str] = set()

        while True:
            # Wall-clock budget
            elapsed = time.monotonic() - start_time
            if elapsed >= self._MAX_WALL_SECONDS:
                print(
                    f"[{self.site_id}] 25-minute wall-clock budget reached "
                    f"({elapsed:.0f}s); stopping cleanly"
                )
                break

            if limit is not None and saved >= limit:
                break

            if page > self._MAX_PAGES:
                print(
                    f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached; stopping"
                )
                break

            # Fetch one INSPIRE results page
            data = self._fetch_inspire_page(page)
            if data is None:
                print(f"[{self.site_id}] failed to fetch INSPIRE page {page}; stopping")
                break

            hits_block = data.get("hits") or {}
            if total_records is None:
                total_records = hits_block.get("total", 0)
                print(
                    f"[{self.site_id}] INSPIRE reports {total_records} "
                    "CDS-linked records (Published Articles)"
                )

            items = hits_block.get("hits") or []
            if not items:
                print(f"[{self.site_id}] page {page}: empty result; done")
                break

            # Progress log every 10 pages (and on page 1)
            if page == 1 or page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                meta = item.get("metadata") or {}
                cds_id: str | None = None

                try:
                    cds_id = self._get_cds_id(meta)
                    if not cds_id:
                        continue

                    cds_url = f"{self.base_url}/record/{cds_id}"

                    # URL deduplication — prevents infinite loops if paginator repeats
                    if cds_url in seen_urls:
                        continue
                    seen_urls.add(cds_url)
                    new_on_page += 1

                    title = self._get_title(meta)
                    if not title:
                        print(f"[{self.site_id}] record {cds_id}: no title, skipping")
                        continue

                    abstract = self._get_abstract(meta)
                    if len(abstract) < self._MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] record {cds_id}: "
                            f"abstract too short ({len(abstract)} chars), skipping"
                        )
                        continue

                    authors = self._get_authors(meta)
                    doi = self._get_doi(meta)
                    journal, volume, issue, pub_year = self._get_pub_info(meta)
                    keywords = self._get_keywords(meta)

                    # Publication date: prefer INSPIRE's earliest_date, fall back to pub year
                    published_date = self._normalize_date(
                        meta.get("earliest_date") or pub_year
                    )

                    # arXiv eprint
                    arxiv_val: str | None = None
                    arxiv_list = meta.get("arxiv_eprints") or []
                    if arxiv_list:
                        arxiv_val = (arxiv_list[0].get("value") or "").strip() or None

                    # Fetch CDS detail page for PDF URL, filename, listing date
                    time.sleep(self._delay)
                    pdf_url, original_filename, listed_date = self._fetch_cds_detail(cds_id)

                    metadata_dict: dict = {
                        "inspire_id": item.get("id"),
                        "cds_id": cds_id,
                        "doi": doi,
                        "journal_raw": journal or None,
                        "volume": volume or None,
                        "issue": issue or None,
                        "originalFilename": original_filename,
                        "arxiv": arxiv_val,
                    }
                    # Drop None values to keep metadata compact
                    metadata_dict = {k: v for k, v in metadata_dict.items() if v is not None}

                    paper = {
                        "site_id": self.site_id,
                        "external_id": cds_id,        # numeric string; used for dedup
                        "post_number": cds_id,         # same; MAX used for incremental crawl
                        "url": cds_url,                # CDS record page → meta_url via adapter
                        "title": title,
                        "abstract": abstract,
                        "authors": authors,            # "; " separated
                        "publisher": "CERN",
                        "journal": journal or None,
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "keywords": keywords or None,  # ", " separated
                        "published_date": published_date,
                        "posted_date": listed_date,    # → listed_date in libertree v2
                        "category": "Published Article",
                        "doi": doi,
                        "metadata": json.dumps(metadata_dict, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[{self.site_id}] saved {saved}/{limit_str}: {title[:70]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    label = cds_id if cds_id else "unknown"
                    print(f"[{self.site_id}] item {label} failed: {exc}")
                    continue

            self._advance_cursor({"page": page + 1}, items_done=len(items))

            # If the entire page was already seen, the paginator is looping — stop
            if new_on_page == 0 and items:
                print(f"[{self.site_id}] page {page}: all items already seen (dedup); stopping")
                break

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
