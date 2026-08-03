# -*- coding: utf-8 -*-
"""Crawler for NLR Reports collection (DSpace 9.1 REST API).

Target: https://reports.nlr.nl/collections/9f55048d-9351-4613-bf4d-e48b90ffe9f3
API root: https://reports.nlr.nl/server/api
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from urllib.parse import unquote, urlparse

import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from crawler.base_crawler import BaseCrawler

_COLLECTION_UUID = "9f55048d-9351-4613-bf4d-e48b90ffe9f3"
_API_BASE = "https://reports.nlr.nl/server"
_PAGE_SIZE = 20
_PAGE_CAP = 200
_CRAWL_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_RETRY_WAITS = (1, 3, 9)
_MIN_ABSTRACT = 50


def _mval(meta: dict, key: str, index: int = 0) -> str | None:
    entries = meta.get(key, [])
    if not entries or index >= len(entries):
        return None
    return entries[index].get("value") or None


def _mvals(meta: dict, key: str) -> list[str]:
    return [e["value"] for e in meta.get(key, []) if e.get("value")]


def _parse_date(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    if m:
        return m.group(1)
    m = re.match(r"^(\d{4})$", raw)
    if m:
        return f"{m.group(1)}-01-01"
    return None


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    tail = unquote(urlparse(url).path.rstrip("/").split("/")[-1])
    return tail[:240] if tail and "." in tail else None


class ReportsNlrNlCollectionsCrawler(BaseCrawler):
    site_id = "reports-nlr-nl-collections"
    site_name = "Custom: reports-nlr-nl-collections"
    base_url = "https://reports.nlr.nl"

    def _api_get(self, url: str, params: dict | None = None) -> dict | None:
        for attempt, wait in enumerate(_RETRY_WAITS, start=1):
            try:
                resp = self._session.get(
                    url,
                    params=params,
                    headers={"Accept": "application/json"},
                    timeout=30,
                    verify=False,
                )
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                print(f"[{self.site_id}] API error attempt {attempt}/3 for {url}: {exc}")
                if attempt < 3:
                    time.sleep(wait)
        return None

    def _get_pdf_info(self, item_uuid: str) -> tuple[str | None, str | None]:
        """Return (pdf_url, original_filename) from the item's ORIGINAL bundle."""
        url = f"{_API_BASE}/api/core/items/{item_uuid}/bundles"
        data = self._api_get(url, params={"embed": "bitstreams"})
        if not data:
            return None, None
        bundles = data.get("_embedded", {}).get("bundles", [])
        for bundle in bundles:
            if bundle.get("name") != "ORIGINAL":
                continue
            bs_data = bundle.get("_embedded", {}).get("bitstreams", {})
            bitstreams = bs_data.get("_embedded", {}).get("bitstreams", [])
            for bs in bitstreams:
                content_href = bs.get("_links", {}).get("content", {}).get("href")
                if content_href:
                    filename = bs.get("name") or _filename_from_url(content_href)
                    return content_href, filename
        return None, None

    def crawl(self, limit=None):
        saved = 0
        page = 0
        seen_uuids: set[str] = set()
        started = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

        search_url = f"{_API_BASE}/api/discover/search/objects"

        while page < _PAGE_CAP:
            if limit is not None and saved >= limit:
                break
            if time.time() - started >= _CRAWL_BUDGET_SECONDS - 30:
                print(f"[{self.site_id}] wall-clock budget nearly reached; stopping cleanly.")
                break
            if page == _PAGE_CAP - 1:
                print(f"[{self.site_id}] safety cap of {_PAGE_CAP} pages reached; logging and exiting.")

            if page % 10 == 0 and page > 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            data = self._api_get(search_url, params={
                "scope": _COLLECTION_UUID,
                "dsoType": "item",
                "size": _PAGE_SIZE,
                "page": page,
                "sort": "dc.date.accessioned,DESC",
            })
            if not data:
                print(f"[{self.site_id}] page {page}: API returned no data. Done.")
                break

            search_result = data.get("_embedded", {}).get("searchResult", {})
            objects = search_result.get("_embedded", {}).get("objects", [])
            page_info = search_result.get("page", {})
            total_pages = page_info.get("totalPages", 0)

            if not objects:
                print(f"[{self.site_id}] page {page}: no records returned. Done.")
                break

            new_items = []
            for obj in objects:
                item = obj.get("_embedded", {}).get("indexableObject", {})
                if not item:
                    continue
                item_uuid = item.get("uuid")
                if not item_uuid or item_uuid in seen_uuids:
                    continue
                seen_uuids.add(item_uuid)
                new_items.append((item_uuid, item))

            if not new_items:
                print(f"[{self.site_id}] page {page}: all records already seen. Done.")
                break

            for item_uuid, item in new_items:
                if limit is not None and saved >= limit:
                    break
                if time.time() - started >= _CRAWL_BUDGET_SECONDS - 30:
                    print(f"[{self.site_id}] wall-clock budget nearly reached; stopping cleanly.")
                    return saved

                try:
                    meta = item.get("metadata", {})

                    abstract = _mval(meta, "dc.description.abstract") or ""
                    if len(abstract) < _MIN_ABSTRACT:
                        print(
                            f"[{self.site_id}] item {item_uuid} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    title = _mval(meta, "dc.title") or item.get("name") or ""
                    if not title:
                        print(f"[{self.site_id}] item {item_uuid} skipped: no title")
                        continue

                    authors_list = _mvals(meta, "dc.contributor.author")
                    authors = "; ".join(authors_list) if authors_list else None

                    issued_raw = _mval(meta, "dc.date.issued")
                    accessioned_raw = _mval(meta, "dc.date.accessioned")
                    published_date = _parse_date(issued_raw)
                    listed_date = _parse_date(accessioned_raw)

                    publisher = _mval(meta, "dc.publisher")

                    kw_list = _mvals(meta, "dc.subject.other") + _mvals(meta, "dc.subject")
                    keywords = ", ".join(kw_list) if kw_list else None

                    # NLR report number (e.g. "NLR-TP-2022-004") as external_id
                    external_id = _mval(meta, "dc.identifier.other") or item_uuid
                    post_number = str(external_id)

                    series_raw = _mval(meta, "dc.relation.ispartofseries")
                    journal = None
                    series = None
                    if series_raw and ";" in series_raw:
                        parts = series_raw.split(";", 1)
                        journal = parts[0].strip()
                        series = parts[1].strip()
                    elif series_raw:
                        series = series_raw

                    doi = _mval(meta, "dc.identifier.doi")
                    if not doi:
                        for uri_val in _mvals(meta, "dc.relation.uri"):
                            if "doi.org" in uri_val:
                                doi = uri_val.split("doi.org/")[-1].strip()
                                break

                    handle = item.get("handle")
                    handle_uri = _mval(meta, "dc.identifier.uri")
                    detail_url = f"{self.base_url}/items/{item_uuid}"

                    # Rate-limit before bundle fetch
                    time.sleep(self._delay)
                    pdf_url, orig_filename = self._get_pdf_info(item_uuid)

                    metadata_dict = {
                        "posted_date": accessioned_raw,
                        "originalFilename": orig_filename,
                        "journal_raw": series_raw,
                        "series": series,
                        "volume": None,
                        "issue": None,
                        "handle": handle,
                        "handle_uri": handle_uri,
                        "item_uuid": item_uuid,
                        "dc.identifier.other": _mval(meta, "dc.identifier.other"),
                        "dc.language.iso": _mval(meta, "dc.language.iso"),
                        "source_list_endpoint": (
                            f"/server/api/discover/search/objects"
                            f"?scope={_COLLECTION_UUID}&dsoType=item&sort=dc.date.accessioned,DESC"
                        ),
                    }

                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": str(external_id),
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "posted_date": listed_date,
                        "authors": authors,
                        "publisher": publisher,
                        "department": None,
                        "journal": journal,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "keywords": keywords,
                        "category": None,
                        "doi": doi,
                        "original_filename": orig_filename,
                        "metadata": json.dumps(metadata_dict, ensure_ascii=False),
                    })
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_or_inf}: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_uuid} failed: {exc}")
                    continue

            if page >= total_pages - 1:
                print(f"[{self.site_id}] page {page}: last page reached. Done.")
                break

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
