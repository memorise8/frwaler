# -*- coding: utf-8 -*-
"""Crawler for data.cso.ie, the CSO Ireland PxStat open-data portal.

Discovered endpoints:
  - landing page: https://data.cso.ie/
  - list API:     POST https://ws.cso.ie/public/api.jsonrpc
                  method=PxStat.Data.Cube_API.ReadCollection
  - detail API:   GET  https://ws.cso.ie/public/api.restful/
                  PxStat.Data.Cube_API.ReadDataset/{matrix}/JSON-stat/2.0/en
  - meta page:    https://data.cso.ie/table/{matrix}

The absolute ``crawler.base_crawler`` import is intentional: site modules are
loaded with ``spec_from_file_location`` and do not have package context.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from html import unescape
from itertools import islice
from urllib.parse import unquote, urlparse

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - crawler can run without bs4
    BeautifulSoup = None

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_THIS_DIR)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402


_JSONRPC_URL = "https://ws.cso.ie/public/api.jsonrpc"
_REST_COLLECTION_URL = (
    "https://ws.cso.ie/public/api.restful/"
    "PxStat.Data.Cube_API.ReadCollection/en"
)
_DATASET_URL_TMPL = (
    "https://ws.cso.ie/public/api.restful/"
    "PxStat.Data.Cube_API.ReadDataset/{matrix}/JSON-stat/2.0/en"
)
_TABLE_URL_TMPL = "https://data.cso.ie/table/{matrix}"
_MAX_PAGES = 200
_PAGE_SIZE = 100
_WALL_BUDGET_SECONDS = 25 * 60
_ABSTRACT_MIN_CHARS = 50
_CURL_WAITS = (1, 3, 9)


class DataCsoIeCrawler(BaseCrawler):
    site_id = "data-cso-ie"
    site_name = "Custom: data-cso-ie"
    base_url = "https://data.cso.ie"

    detail_delay = 1.0

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(
        self,
        url: str,
        *,
        accept: str = "application/json, text/html;q=0.9, */*;q=0.8",
        retries: int = 3,
        max_time: int = 60,
    ) -> str | None:
        """GET with curl, TLS cap, retries, and replacement decoding."""
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--max-time",
            str(max_time),
            "-A",
            self.USER_AGENT,
            "-H",
            f"Accept: {accept}",
            url,
        ]
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=max_time + 10,
                )
                text = self._decode(result.stdout)
                if result.returncode == 0 and text.strip():
                    return text
                err = self._decode(result.stderr).strip()
                print(
                    f"[{self.site_id}] curl GET failed "
                    f"(attempt {attempt + 1}/{retries}) for {url}: "
                    f"code={result.returncode} {err[:200]}"
                )
            except Exception as exc:
                print(
                    f"[{self.site_id}] curl GET error "
                    f"(attempt {attempt + 1}/{retries}) for {url}: {exc}"
                )
            if attempt < retries - 1:
                time.sleep(_CURL_WAITS[attempt])
        print(f"[{self.site_id}] all {retries} GET attempts failed for {url}")
        return None

    def _curl_post_jsonrpc(
        self,
        method: str,
        params: dict,
        *,
        retries: int = 3,
        max_time: int = 120,
    ) -> str | None:
        """POST JSON-RPC with curl and exponential backoff."""
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            ensure_ascii=False,
        )
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--max-time",
            str(max_time),
            "-A",
            self.USER_AGENT,
            "-H",
            "Content-Type: application/json",
            "-H",
            "Accept: application/json, */*",
            "-d",
            body,
            _JSONRPC_URL,
        ]
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=max_time + 10,
                )
                text = self._decode(result.stdout)
                if result.returncode == 0 and text.strip():
                    return text
                err = self._decode(result.stderr).strip()
                print(
                    f"[{self.site_id}] curl POST failed "
                    f"(attempt {attempt + 1}/{retries}) method={method}: "
                    f"code={result.returncode} {err[:200]}"
                )
            except Exception as exc:
                print(
                    f"[{self.site_id}] curl POST error "
                    f"(attempt {attempt + 1}/{retries}) method={method}: {exc}"
                )
            if attempt < retries - 1:
                time.sleep(_CURL_WAITS[attempt])
        print(f"[{self.site_id}] all {retries} POST attempts failed for {method}")
        return None

    @staticmethod
    def _decode(raw: bytes | None) -> str:
        if not raw:
            return ""
        for encoding in ("utf-8-sig", "utf-16", "latin-1"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _make_soup(raw: str):
        """Create BeautifulSoup with html5lib -> lxml -> html.parser fallback."""
        if BeautifulSoup is None:
            return None
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            print(f"[data-cso-ie] BeautifulSoup failed for all parsers: {last_exc}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_text(value) -> str:
        if value is None:
            return ""
        if isinstance(value, (list, tuple)):
            value = " ".join(str(v) for v in value if v not in (None, ""))
        text = str(value)
        text = re.sub(r"\[url=[^\]]*\]", "", text, flags=re.I)
        text = re.sub(r"\[/url\]", "", text, flags=re.I)
        text = re.sub(r"\[[^\]]+\]", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _date_only(value) -> str | None:
        if value in (None, ""):
            return None
        match = re.search(r"\d{4}-\d{2}-\d{2}", str(value))
        return match.group(0) if match else None

    @staticmethod
    def _filename_from_url(url: str | None) -> str | None:
        if not url:
            return None
        tail = unquote(urlparse(url).path.rstrip("/").split("/")[-1])
        if "." in tail and len(tail) <= 255:
            return tail
        return None

    @staticmethod
    def _chunks(items: list[dict], size: int):
        iterator = iter(items)
        while True:
            chunk = list(islice(iterator, size))
            if not chunk:
                return
            yield chunk

    @staticmethod
    def _without_large_values(detail: dict) -> dict:
        """Keep raw metadata fields without storing the whole statistical cube."""
        if not isinstance(detail, dict):
            return {}
        pruned = {}
        for key, value in detail.items():
            if key == "value":
                pruned["value_omitted_count"] = len(value) if hasattr(value, "__len__") else None
            elif key == "status":
                pruned["status_omitted_count"] = len(value) if hasattr(value, "__len__") else None
            else:
                pruned[key] = value
        return pruned

    @classmethod
    def _dimension_labels(cls, detail: dict) -> list[str]:
        labels = []
        dimensions = detail.get("dimension") if isinstance(detail.get("dimension"), dict) else {}
        for dim_id in detail.get("id") or []:
            dim_id_text = cls._clean_text(dim_id)
            if not dim_id_text or dim_id_text == "STATISTIC" or dim_id_text.startswith("TLIST"):
                continue
            dim_info = dimensions.get(dim_id) if isinstance(dimensions.get(dim_id), dict) else {}
            label = cls._clean_text(dim_info.get("label")) or dim_id_text
            labels.append(label)
        return labels

    def _fetch_collection(self) -> list[dict]:
        """Fetch the PxStat collection list, falling back from JSON-RPC to REST."""
        landing = self._curl_get(self.base_url + "/", accept="text/html, */*", max_time=30)
        if landing:
            self._make_soup(landing)

        raw = self._curl_post_jsonrpc(
            "PxStat.Data.Cube_API.ReadCollection",
            {"LngIsoCode": "en"},
            max_time=120,
        )
        if not raw:
            print(f"[{self.site_id}] JSON-RPC collection failed; trying REST collection")
            raw = self._curl_get(_REST_COLLECTION_URL, accept="application/json, */*", max_time=120)
        if not raw:
            return []

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] collection JSON decode failed: {exc}")
            return []

        collection = data.get("result") if isinstance(data, dict) else None
        if collection is None:
            collection = data
        if not isinstance(collection, dict):
            print(f"[{self.site_id}] unexpected collection payload: {type(collection).__name__}")
            return []

        items = collection.get("link", {}).get("item", [])
        if not isinstance(items, list):
            print(f"[{self.site_id}] collection item list missing")
            return []

        records = []
        seen_matrices = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            ext = item.get("extension") if isinstance(item.get("extension"), dict) else {}
            matrix = self._clean_text(ext.get("matrix"))
            if not matrix or matrix in seen_matrices:
                continue
            seen_matrices.add(matrix)
            records.append(
                {
                    "matrix": matrix,
                    "label": self._clean_text(item.get("label")),
                    "updated_raw": item.get("updated"),
                    "href": item.get("href"),
                    "raw": item,
                }
            )
        return records

    def _fetch_detail(self, matrix: str, href: str | None) -> tuple[dict | None, str | None]:
        urls = [_DATASET_URL_TMPL.format(matrix=matrix)]
        if href and href not in urls:
            urls.append(str(href))

        for url in urls:
            raw = self._curl_get(url, accept="application/json, */*", max_time=75)
            if not raw:
                continue
            try:
                detail = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"[{self.site_id}] {matrix}: detail JSON decode failed at {url}: {exc}")
                continue
            if isinstance(detail, dict) and detail.get("class") == "dataset":
                return detail, url
            print(f"[{self.site_id}] {matrix}: unexpected detail payload at {url}")
        return None, None

    def _build_abstract(
        self,
        *,
        title: str,
        notes: list,
        subject: str | None,
        product: str | None,
        dimensions: list[str],
        updated: str | None,
    ) -> str:
        note_text = self._clean_text(notes)
        parts = []
        if note_text:
            parts.append(note_text)
        if subject:
            parts.append(f"Subject area: {subject}.")
        if product:
            parts.append(f"Statistical product: {product}.")

        dim_labels = [d for d in dimensions if d and not d.startswith("TLIST") and d != "STATISTIC"]
        if dim_labels:
            parts.append(f"Dimensions: {', '.join(dim_labels)}.")
        if updated:
            parts.append(f"Dataset last updated by the CSO on {updated}.")

        abstract = " ".join(parts).strip()
        if len(abstract) < 100:
            prefix = (
                f"This CSO Ireland PxStat dataset, {title}, contains official "
                "statistics published by the Central Statistics Office. "
            )
            abstract = (prefix + abstract).strip()
        if len(abstract) < 100:
            abstract = (
                abstract
                + " The table metadata identifies the subject area, statistical product, "
                "dimensions, update date, and source contact where CSO provides them."
            ).strip()
        return abstract

    def _build_record(self, item: dict, detail: dict, detail_api_url: str | None) -> dict | None:
        matrix = item["matrix"]
        ext = detail.get("extension") if isinstance(detail.get("extension"), dict) else {}

        subject_info = ext.get("subject") if isinstance(ext.get("subject"), dict) else {}
        product_info = ext.get("product") if isinstance(ext.get("product"), dict) else {}
        contact_info = ext.get("contact") if isinstance(ext.get("contact"), dict) else {}
        copyright_info = ext.get("copyright") if isinstance(ext.get("copyright"), dict) else {}

        title = self._clean_text(detail.get("label") or item.get("label") or f"Dataset {matrix}")
        if not title:
            print(f"[{self.site_id}] {matrix}: missing title, skipping")
            return None

        updated_raw = detail.get("updated") or item.get("updated_raw")
        published_date = self._date_only(updated_raw)
        listed_date = self._date_only(item.get("updated_raw") or updated_raw)
        subject = self._clean_text(subject_info.get("value")) or None
        product = self._clean_text(product_info.get("value")) or None
        dimension_ids = [self._clean_text(v) for v in (detail.get("id") or []) if self._clean_text(v)]
        dimension_labels = self._dimension_labels(detail)

        abstract = self._build_abstract(
            title=title,
            notes=detail.get("note") or [],
            subject=subject,
            product=product,
            dimensions=dimension_labels,
            updated=published_date,
        )
        if len(abstract) < _ABSTRACT_MIN_CHARS:
            print(f"[{self.site_id}] {matrix}: abstract too short ({len(abstract)} chars), skipping")
            return None

        pdf_url = None
        original_filename = self._filename_from_url(pdf_url)
        page_url = _TABLE_URL_TMPL.format(matrix=matrix)
        publisher_raw = self._clean_text(copyright_info.get("name")) or "Central Statistics Office, Ireland"
        publisher = "Central Statistics Office" if publisher_raw == "Central Statistics Office, Ireland" else publisher_raw
        keywords = ", ".join(dict.fromkeys(d for d in dimension_labels if d)) or None

        metadata = {
            "posted_date": item.get("updated_raw") or updated_raw,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": product,
            "volume": None,
            "issue": None,
            "matrix": matrix,
            "matrix_id": matrix,
            "collection_href": item.get("href"),
            "detail_api_url": detail_api_url,
            "subject_code": subject_info.get("code"),
            "subject_value": subject,
            "product_code": product_info.get("code"),
            "product_value": product,
            "contact_name": contact_info.get("name"),
            "contact_email": contact_info.get("email"),
            "contact_phone": contact_info.get("phone"),
            "publisher_raw": publisher_raw,
            "copyright_code": copyright_info.get("code"),
            "copyright_href": copyright_info.get("href"),
            "exceptional": ext.get("exceptional"),
            "official": ext.get("official"),
            "archive": ext.get("archive"),
            "language": ext.get("language"),
            "dimension_ids": dimension_ids,
            "dimension_labels": dimension_labels,
            "notes_raw": detail.get("note") or [],
            "collection_item": item.get("raw"),
            "detail_raw": self._without_large_values(detail),
        }

        return {
            "id": f"{self.site_id}:{matrix}",
            "site_id": self.site_id,
            "external_id": matrix,
            "post_number": matrix,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": None,
            "publisher": publisher,
            "department": subject,
            "journal": None,
            "url": page_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": subject,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        if limit is not None:
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                print(f"[{self.site_id}] invalid limit {limit!r}; treating as unlimited")
                limit = None
        if limit is not None and limit <= 0:
            print(f"[{self.site_id}] limit <= 0, nothing to crawl")
            return 0

        limit_or_inf = str(limit) if limit is not None else "inf"
        collection = self._fetch_collection()
        print(f"[{self.site_id}] found {len(collection)} datasets in collection")
        if not collection:
            return 0

        saved = 0
        seen_urls = set()

        try:
            for page_number, page_items in enumerate(self._chunks(collection, _PAGE_SIZE), start=1):
                if page_number > _MAX_PAGES:
                    print(f"[{self.site_id}] reached safety page cap {_MAX_PAGES}; stopping")
                    break
                if not page_items:
                    print(f"[{self.site_id}] page {page_number}: no records, stopping")
                    break

                if page_number % 10 == 0:
                    print(f"[{self.site_id}] page {page_number}: saved {saved}/{limit_or_inf}")

                new_on_page = 0
                for item in page_items:
                    if limit is not None and saved >= limit:
                        break
                    elapsed = time.time() - start_time
                    if elapsed >= (_WALL_BUDGET_SECONDS - 30):
                        print(
                            f"[{self.site_id}] approaching 25 minute wall-clock budget "
                            f"after {saved} saves; exiting cleanly"
                        )
                        return saved

                    matrix = item["matrix"]
                    page_url = _TABLE_URL_TMPL.format(matrix=matrix)
                    if page_url in seen_urls:
                        continue
                    seen_urls.add(page_url)
                    new_on_page += 1

                    try:
                        detail, detail_api_url = self._fetch_detail(matrix, item.get("href"))
                        if detail is None:
                            print(f"[{self.site_id}] item {matrix} failed: detail fetch failed")
                            continue
                        record = self._build_record(item, detail, detail_api_url)
                        if record is None:
                            continue
                        self._save_paper(record)
                        saved += 1
                    except Exception as exc:
                        print(f"[{self.site_id}] item {matrix} failed: {exc}")
                        continue
                    finally:
                        time.sleep(float(getattr(self, "detail_delay", 1.0)))

                if limit is not None and saved >= limit:
                    break
                if new_on_page == 0:
                    print(f"[{self.site_id}] page {page_number}: 0 new records, stopping")
                    break
        except KeyboardInterrupt:
            raise

        print(f"[{self.site_id}] done. saved {saved} records.")
        return saved
