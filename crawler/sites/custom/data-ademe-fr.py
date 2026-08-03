# -*- coding: utf-8 -*-
"""Crawler for data.ademe.fr (ADEME open-data portal).

Starting URL: https://data.ademe.fr/

Access strategy
---------------
The site is a Nuxt SPA whose data actually lives behind an embedded
"data-fair" (Etalab/Koumoul) backend mounted at ``/data-fair``. The
homepage's own top-level ``/api/v1/...`` routes (Express) only serve static
portal assets (logo, fonts) and 404 with a bare ``unknown api endpoint``
body for anything else — the real catalog API is:

    GET https://data.ademe.fr/data-fair/api/v1/datasets
        ?status=finalized&size=N&page=P&sort=createdAt:-1
        &select=id,slug,title,description,keywords,topics,owner,license,
                frequency,spatial,createdAt,updatedAt,dataUpdatedAt,
                finalizedAt,file,originalFile,schema

A single listing call returns everything needed per item (no per-item
detail fetch required): description, schema (column titles), owner,
license, frequency, spatial coverage, and the original/converted file
metadata used to build a download URL:

    GET https://data.ademe.fr/data-fair/api/v1/datasets/{slug}/data-files/{filename}

Each "paper" here is a dataset entry rather than a PDF; the abstract is
built from the dataset description enriched with its schema field titles
and coverage/frequency/license metadata so it stays well above the
minimum length even when the raw description is short or empty.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

# Absolute-import safety (spec_from_file_location has no package context)
_pkg_root = Path(__file__).resolve().parents[3]
if str(_pkg_root) not in sys.path:
    sys.path.insert(0, str(_pkg_root))

from crawler.base_crawler import BaseCrawler  # noqa: E402

_BASE = "https://data.ademe.fr"
_API_BASE = f"{_BASE}/data-fair/api/v1/datasets"
_SELECT_FIELDS = (
    "id,slug,title,description,keywords,topics,owner,license,frequency,"
    "spatial,createdAt,updatedAt,dataUpdatedAt,finalizedAt,file,originalFile,schema"
)


class DataAdemeFrCrawler(BaseCrawler):

    site_id = "data-ademe-fr"
    site_name = "Custom: data-ademe-fr"
    base_url = _BASE

    _PAGE_SIZE = 40
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _CRAWL_BUDGET_SECS = 1500  # 25-minute wall-clock limit
    _MIN_ABSTRACT_LEN = 50

    # ------------------------------------------------------------------ #
    # Low-level curl JSON fetch
    # ------------------------------------------------------------------ #

    def _curl_json(self, url: str):
        """Fetch a URL via curl and parse it as JSON.

        Retries up to 3 times with 1s/3s/9s backoff. Returns the parsed
        object, or ``None`` if all attempts fail.
        """
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", "60",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: application/json",
            url,
        ]
        for attempt in range(3):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=70)
                raw = r.stdout.decode("utf-8", errors="replace")
                if raw:
                    return json.loads(raw)
            except Exception as exc:
                print(f"[{self.site_id}] fetch error (attempt {attempt + 1}/3) for {url}: {exc}")
            if attempt < 2:
                time.sleep(3 ** attempt)  # 1s, 3s
        return None

    def _fetch_page(self, page: int):
        url = (
            f"{_API_BASE}?status=finalized&size={self._PAGE_SIZE}&page={page}"
            f"&sort=createdAt:-1&select={quote(_SELECT_FIELDS, safe=',')}"
        )
        return self._curl_json(url)

    # ------------------------------------------------------------------ #
    # Item -> paper mapping
    # ------------------------------------------------------------------ #

    @staticmethod
    def _iso_date(value) -> str | None:
        if not value or not isinstance(value, str) or len(value) < 10:
            return None
        return value[:10]

    @staticmethod
    def _pick_file(item: dict) -> dict | None:
        """Prefer the original uploaded file over the converted one."""
        return item.get("originalFile") or item.get("file")

    def _build_abstract(self, item: dict) -> str:
        parts = []

        description = (item.get("description") or "").strip()
        if description:
            parts.append(description)

        schema = item.get("schema") or []
        field_labels = [
            (f.get("title") or f.get("key"))
            for f in schema
            if isinstance(f, dict) and not f.get("x-calculated") and (f.get("title") or f.get("key"))
        ]
        if field_labels:
            parts.append("Variables du jeu de données : " + ", ".join(field_labels))

        meta_bits = []
        spatial = item.get("spatial")
        if spatial:
            meta_bits.append(f"Couverture géographique : {spatial}")
        frequency = item.get("frequency")
        if frequency:
            meta_bits.append(f"Fréquence de mise à jour : {frequency}")
        license_info = item.get("license") or {}
        if license_info.get("title"):
            meta_bits.append(f"Licence : {license_info['title']}")
        owner = item.get("owner") or {}
        if owner.get("name"):
            org_bit = f"Organisme producteur : {owner['name']}"
            if owner.get("departmentName"):
                org_bit += f" ({owner['departmentName']})"
            meta_bits.append(org_bit)
        if meta_bits:
            parts.append(" ".join(meta_bits))

        keywords = item.get("keywords") or []
        if keywords:
            parts.append("Mots-clés : " + ", ".join(k for k in keywords if k))

        return "\n\n".join(p for p in parts if p).strip()

    def _build_paper(self, item: dict) -> dict | None:
        slug = item.get("slug")
        title = (item.get("title") or "").strip()
        if not slug or not title:
            return None

        meta_url = item.get("page") or f"{_BASE}/datasets/{slug}"

        abstract = self._build_abstract(item)
        if len(abstract) < self._MIN_ABSTRACT_LEN:
            print(f"[{self.site_id}] skip '{title[:50]}': abstract too short ({len(abstract)} chars)")
            return None

        picked_file = self._pick_file(item)
        pdf_url = None
        original_filename = None
        if picked_file and picked_file.get("name"):
            original_filename = picked_file["name"]
            pdf_url = f"{_API_BASE}/{slug}/data-files/{quote(original_filename)}"

        owner = item.get("owner") or {}
        topics = item.get("topics") or []
        license_info = item.get("license") or {}
        keywords = item.get("keywords") or []

        published_date = self._iso_date(item.get("createdAt"))
        listed_date = self._iso_date(item.get("updatedAt")) or published_date

        metadata = {
            "posted_date": item.get("updatedAt"),
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "dataset_id": item.get("id"),
            "slug": slug,
            "license": license_info.get("title"),
            "frequency": item.get("frequency"),
            "spatial": item.get("spatial"),
            "topics": [t.get("title") for t in topics if isinstance(t, dict) and t.get("title")],
            "keywords_raw": keywords,
            "owner_department_code": owner.get("department"),
            "owner_department_name": owner.get("departmentName"),
            "finalizedAt": item.get("finalizedAt"),
            "dataUpdatedAt": item.get("dataUpdatedAt"),
            "schema_field_count": len(item.get("schema") or []),
        }

        return {
            "external_id": item.get("id") or slug,
            "post_number": slug,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            # top-level ``posted_date`` (not ``listed_date``) is what
            # libertree_adapter.paper_to_document() actually reads to
            # populate documents.posted_date/listed_date; metadata's
            # posted_date above keeps the raw timestamp per spec.
            "posted_date": listed_date,
            "authors": "",
            "publisher": owner.get("name") or "",
            "department": owner.get("departmentName") or "",
            "journal": "",
            "url": meta_url,
            "pdf_url": pdf_url,
            "keywords": ", ".join(k for k in keywords if k),
            "category": topics[0].get("title") if topics and isinstance(topics[0], dict) else "",
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------ #
    # Main crawl entry point
    # ------------------------------------------------------------------ #

    def crawl(self, limit=None) -> int:
        start_ts = time.monotonic()
        saved = 0
        seen_urls: set[str] = set()
        limit_str = str(limit) if limit is not None else "inf"

        print(f"[{self.site_id}] Starting crawl (limit={limit_str})")

        page = 1
        while True:
            if time.monotonic() - start_ts > self._CRAWL_BUDGET_SECS:
                print(f"[{self.site_id}] 25-min budget reached at page {page}; stopping.")
                break
            if limit is not None and saved >= limit:
                break
            if page > self._MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached; stopping.")
                break

            if page % 10 == 0 or page == 1:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            data = self._fetch_page(page)
            if not data or not isinstance(data, dict):
                print(f"[{self.site_id}] page {page}: fetch failed; stopping.")
                break

            results = data.get("results") or []
            if not results:
                print(f"[{self.site_id}] page {page}: no results; end of pagination.")
                break

            new_on_page = 0
            for item in results:
                if limit is not None and saved >= limit:
                    break

                meta_url = item.get("page") or ""
                if not meta_url or meta_url in seen_urls:
                    continue
                seen_urls.add(meta_url)
                new_on_page += 1

                try:
                    paper = self._build_paper(item)
                    if not paper:
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved ({saved}/{limit_str}): {paper['title'][:70]}")
                    time.sleep(self._delay)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item '{item.get('title', '?')[:50]}' failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: all duplicates; stopping.")
                break

            page += 1
            time.sleep(self._delay)

        print(f"[{self.site_id}] Crawl complete: saved={saved}")
        return saved
