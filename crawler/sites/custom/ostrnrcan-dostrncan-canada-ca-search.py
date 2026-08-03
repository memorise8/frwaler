# -*- coding: utf-8 -*-
"""Crawler for the NRCan Open S&T Repository (OSTR/DOST) search."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from html import unescape
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class OstrnrcanDostrncanCanadaCaSearchCrawler(BaseCrawler):
    site_id = "ostrnrcan-dostrncan-canada-ca-search"
    site_name = "Custom: ostrnrcan-dostrncan-canada-ca-search"
    base_url = "https://ostrnrcan-dostrncan.canada.ca"

    # The Angular frontend is served from base_url, but the actual DSpace 7
    # REST backend lives on a separate Azure host (discovered by grepping the
    # frontend's main-*.js bundle for "baseUrl:").
    REST_BASE = "https://ostr-backend-prod.azure.cloud.nrcan-rncan.gc.ca/server"
    LIST_API_URL = f"{REST_BASE}/api/discover/search/objects"
    ITEM_API_URL = f"{REST_BASE}/api/core/items/{{uuid}}"

    START_URL = (
        f"{base_url}/search?page=1&query=*&spc.sf=dc.date.issued"
        "&spc.sd=DESC&spc.page=1&f.itemtype_en=Report,equals"
    )

    PAGE_SIZE = 20
    SAFETY_PAGE_CAP = 200
    TIME_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    BACKOFF_SECONDS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 100

    def crawl(self, limit=None):
        start_time = time.monotonic()
        saved = 0
        page = 0
        item_number = 0
        seen_urls = set()

        while True:
            if limit is not None and saved >= limit:
                break

            if page >= self.SAFETY_PAGE_CAP:
                print(f"[{self.site_id}] reached safety cap of {self.SAFETY_PAGE_CAP} pages; stopping")
                break

            elapsed = time.monotonic() - start_time
            if elapsed > self.TIME_BUDGET_SECONDS:
                print(f"[{self.site_id}] time budget ({self.TIME_BUDGET_SECONDS}s) exceeded; stopping cleanly")
                break

            if page > 0 and page % 10 == 0:
                limit_display = limit if limit is not None else "inf"
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_display}")

            list_data = self._fetch_list(page)
            if not list_data:
                print(f"[{self.site_id}] list API failed at page {page + 1}; stopping")
                break

            records = self._list_records(list_data)
            if not records:
                print(f"[{self.site_id}] no records returned at page {page + 1}; stopping")
                break

            page_info = self._page_meta(list_data)
            total = page_info.get("totalElements")
            print(
                f"[{self.site_id}] page {page + 1}: discovered {len(records)} records"
                + (f" of {total}" if total is not None else "")
            )

            new_on_page = 0
            for record in records:
                if limit is not None and saved >= limit:
                    break

                item_number += 1
                try:
                    list_item = self._indexable_object(record)
                    uuid = (list_item.get("uuid") or list_item.get("id") or "").strip()
                    if not uuid:
                        raise RuntimeError("list record has no item uuid")

                    handle = list_item.get("handle")
                    item_url = self._item_url(handle, uuid)
                    if item_url in seen_urls:
                        continue
                    seen_urls.add(item_url)
                    new_on_page += 1

                    time.sleep(self._delay)

                    detail = self._fetch_item(uuid)
                    if not detail:
                        raise RuntimeError("item detail API failed after retries")

                    bitstreams = self._fetch_bitstreams(detail, uuid)
                    parsed = self._parse_item(detail, bitstreams, item_url)

                    abstract = parsed["abstract"]
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {item_number} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_number} skipped: "
                            f"abstract below save threshold ({len(abstract)} chars)"
                        )
                        continue

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": parsed["post_number"],
                        "title": parsed["title"],
                        "authors": json.dumps(parsed["authors"], ensure_ascii=False),
                        "abstract": abstract,
                        "category": parsed["category"],
                        "keywords": json.dumps(parsed["keywords"], ensure_ascii=False),
                        "published_date": parsed["published_date"],
                        "url": parsed["url"],
                        "pdf_url": parsed["pdf_url"],
                        "doi": parsed["doi"],
                        "department": parsed["publisher"],
                        "metadata": json.dumps(parsed["metadata"], ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {parsed['title'][:90]}")
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_number} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page + 1}: all records already seen; stopping")
                break

            total_pages = page_info.get("totalPages")
            if total_pages is not None and page + 1 >= int(total_pages):
                print(f"[{self.site_id}] reached last page ({total_pages}); stopping")
                break
            if len(records) < self.PAGE_SIZE:
                break
            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # List / detail fetching
    # ------------------------------------------------------------------

    def _fetch_list(self, page):
        params = {
            "query": "*",
            "page": str(page),
            "size": str(self.PAGE_SIZE),
            "sort": "dc.date.issued,DESC",
            "f.itemtype_en": "Report,equals",
        }
        url = f"{self.LIST_API_URL}?{urlencode(params)}"
        return self._curl_json(url, context=f"list page={page + 1}")

    def _fetch_item(self, uuid):
        return self._curl_json(
            self.ITEM_API_URL.format(uuid=uuid),
            context=f"item {uuid} detail",
        )

    def _fetch_bitstreams(self, item, uuid):
        bundles_url = (
            item.get("_links", {}).get("bundles", {}).get("href")
            or f"{self.ITEM_API_URL.format(uuid=uuid)}/bundles"
        )
        bundles_data = self._curl_json(bundles_url, context=f"item {uuid} bundles")
        bundles = bundles_data.get("_embedded", {}).get("bundles", []) if bundles_data else []
        if not isinstance(bundles, list):
            return []

        ordered = sorted(bundles, key=lambda b: 0 if b.get("name") == "ORIGINAL" else 1)
        bitstreams = []
        for bundle in ordered:
            href = bundle.get("_links", {}).get("bitstreams", {}).get("href")
            if not href:
                continue
            data = self._curl_json(
                href,
                context=f"item {uuid} bundle {bundle.get('name') or bundle.get('uuid')} bitstreams",
            )
            if not data:
                continue
            found = data.get("_embedded", {}).get("bitstreams", [])
            if isinstance(found, list):
                bitstreams.extend(found)
        return bitstreams

    def _curl_json(self, url, context="request"):
        raw = self._curl_get(
            url,
            context=context,
            accept="application/json,text/javascript,*/*;q=0.8",
        )
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] {context} invalid JSON: {exc}")
            return None
        if not isinstance(data, dict):
            print(f"[{self.site_id}] {context} JSON root is not an object")
            return None
        return data

    def _curl_get(self, url, context="request", accept=None):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--fail",
            "--compressed",
            "--max-time",
            "45",
            "--connect-timeout",
            "15",
            "-A",
            self.USER_AGENT,
            "-H",
            f"Accept: {accept or 'application/json,text/html,*/*;q=0.8'}",
            "-H",
            "Accept-Language: en-CA,en;q=0.9,fr-CA;q=0.8,fr;q=0.7",
            "-H",
            f"Referer: {self.START_URL}",
            url,
        ]

        last_error = ""
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=55)
                stdout = result.stdout or b""
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                if result.returncode == 0 and stdout.strip():
                    return stdout.decode("utf-8", errors="replace")
                last_error = stderr or f"curl exit {result.returncode}; empty response"
            except subprocess.TimeoutExpired as exc:
                last_error = f"curl timeout after {exc.timeout}s"
            except Exception as exc:
                last_error = str(exc)

            wait = self.BACKOFF_SECONDS[attempt]
            print(
                f"[{self.site_id}] {context} curl failed "
                f"(attempt {attempt + 1}/3): {last_error}"
            )
            if attempt < 2:
                print(f"[{self.site_id}] retrying in {wait}s...")
                time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_item(self, item, bitstreams, item_url):
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        uuid = (item.get("uuid") or item.get("id") or "").strip()
        if not uuid:
            raise RuntimeError("detail record has no uuid")
        handle = item.get("handle")

        title = self._metadata_first(metadata, ("dc.title",), preferred_languages=("en", "fr", None))
        title = title or self._clean_text(item.get("name") or "")
        if not title:
            raise RuntimeError(f"{uuid} has no title")

        abstract = self._metadata_first(
            metadata,
            ("dc.description.abstract", "dcterms.abstract"),
            preferred_languages=("en", "fr", None),
        )
        if len(abstract) < 50:
            fallback = self._metadata_first(
                metadata,
                ("dc.description",),
                preferred_languages=("en", "fr", None),
            )
            if fallback:
                abstract = fallback
        abstract = self._clean_text(abstract)

        authors = self._metadata_values(
            metadata,
            ("nrcan.contributor.corporateauthor", "dc.contributor.author", "dc.creator"),
        )
        publisher = self._metadata_values(metadata, ("dc.publisher",), preferred_languages=("en", "fr", None))
        department = self._metadata_values(metadata, ("nrcan.division.name",), preferred_languages=("en", "fr", None))
        journal = self._metadata_first(metadata, ("nrcan.journal.title",))

        keywords = self._dedupe(
            self._metadata_values(metadata, ("dc.subject.gc_en",), preferred_languages=("en", None))
            + self._metadata_values(metadata, ("dc.subject.broad_en",), preferred_languages=("en", None))
            + self._metadata_values(metadata, ("dc.subject.descriptor_en",), preferred_languages=("en", None))
            + self._metadata_values(metadata, ("dc.subject.geoscan_en",), preferred_languages=("en", None))
            + self._metadata_values(metadata, ("dc.subject.cfs_en",), preferred_languages=("en", None))
        )
        category = (
            self._metadata_first(metadata, ("dc.type_en",))
            or self._metadata_first(metadata, ("dc.type",))
        )

        published_date = self._normalize_date(
            self._metadata_first(metadata, ("dc.date.issued",))
        )
        posted_date_raw = self._metadata_first(
            metadata, ("dc.date.available", "dc.date.accessioned")
        )

        doi = self._normalize_doi(self._metadata_first(metadata, ("dc.identifier.doi",)))

        pdf_url, original_filename, file_summaries = self._select_file(bitstreams)

        post_number = self._post_number_from_handle(handle) or uuid

        metadata_summary = {
            "posted_date": posted_date_raw,
            "originalFilename": original_filename,
            "journal_raw": journal,
            "series": self._metadata_first(metadata, ("nrcan.reportnumber",)),
            "volume": None,
            "issue": self._metadata_first(metadata, ("nrcan.issue",)),
            "node_id": uuid,
            "handle": handle,
            "start_url": self.START_URL,
            "list_api": self.LIST_API_URL,
            "detail_api": self.ITEM_API_URL.format(uuid=uuid),
            "type": item.get("type"),
            "doi": doi,
            "catalogue_number": self._metadata_first(metadata, ("dc.identifier.catn",)),
            "isbn": self._metadata_first(metadata, ("dc.identifier.isbn",)),
            "citation": self._metadata_first(metadata, ("dc.identifier.citation",)),
            "division_sector": self._metadata_first(metadata, ("nrcan.division.sector",)),
            "openaccess": self._metadata_first(metadata, ("nrcan.openaccess",)),
            "filetype": self._metadata_first(metadata, ("nrcan.filetype",)),
            "language": self._metadata_first(metadata, ("dc.language",)),
            "bitstreams": file_summaries,
            "metadata_keys": sorted(metadata.keys()),
        }

        return {
            "post_number": post_number,
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "category": category,
            "keywords": keywords,
            "published_date": published_date,
            "url": item_url,
            "pdf_url": pdf_url,
            "doi": doi,
            "publisher": self._dedupe(publisher + department),
            "metadata": metadata_summary,
        }

    def _select_file(self, bitstreams):
        summaries = []
        for bitstream in bitstreams:
            name = self._clean_text(bitstream.get("name") or "")
            content_url = bitstream.get("_links", {}).get("content", {}).get("href") or ""
            bundle_name = bitstream.get("bundleName")
            summary = {
                "uuid": bitstream.get("uuid") or bitstream.get("id"),
                "name": name,
                "bundleName": bundle_name,
                "sizeBytes": bitstream.get("sizeBytes"),
                "content_url": content_url,
                "checksum": (bitstream.get("checkSum") or {}).get("value"),
            }
            if name or content_url:
                summaries.append(summary)

        for summary in summaries:
            if summary["name"].lower().endswith(".pdf"):
                return summary["content_url"], (summary["name"] or None), summaries
        for summary in summaries:
            if summary["content_url"]:
                return summary["content_url"], (summary["name"] or None), summaries
        return "", None, summaries

    def _item_url(self, handle, uuid):
        if handle:
            return f"{self.base_url}/handle/{handle}"
        return f"{self.base_url}/items/{uuid}"

    @staticmethod
    def _post_number_from_handle(handle):
        if not handle:
            return None
        tail = str(handle).rstrip("/").split("/")[-1]
        if tail.isdigit():
            return tail
        return str(handle)

    def _make_soup(self, raw):
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                last_exc = exc
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed: {exc}")
        print(f"[{self.site_id}] all BeautifulSoup parsers failed: {last_exc}")
        return None

    def _list_records(self, data):
        records = (
            data.get("_embedded", {})
            .get("searchResult", {})
            .get("_embedded", {})
            .get("objects", [])
        )
        return records if isinstance(records, list) else []

    def _page_meta(self, data):
        page_meta = data.get("_embedded", {}).get("searchResult", {}).get("page", {})
        return page_meta if isinstance(page_meta, dict) else {}

    def _indexable_object(self, record):
        obj = record.get("_embedded", {}).get("indexableObject", {})
        return obj if isinstance(obj, dict) else {}

    def _metadata_first(self, metadata, keys, preferred_languages=None):
        values = self._metadata_values(metadata, keys, preferred_languages=preferred_languages)
        return values[0] if values else ""

    def _metadata_values(self, metadata, keys, preferred_languages=None):
        all_values = []
        for key in keys:
            entries = metadata.get(key)
            if not entries:
                continue
            if isinstance(entries, dict):
                entries = [entries]
            if not isinstance(entries, list):
                entries = [{"value": entries}]
            entries = sorted(entries, key=lambda e: e.get("place", 0) if isinstance(e, dict) else 0)

            if preferred_languages:
                selected = []
                for lang in preferred_languages:
                    for entry in entries:
                        if not isinstance(entry, dict):
                            continue
                        entry_lang = entry.get("language")
                        if lang is None:
                            if entry_lang in (None, ""):
                                selected.append(entry)
                        elif str(entry_lang or "").lower() == str(lang).lower():
                            selected.append(entry)
                    if selected:
                        all_values.extend(selected)
                        break
                if not selected:
                    all_values.extend(entries)
            else:
                all_values.extend(entries)

        values = []
        for entry in all_values:
            value = entry.get("value") if isinstance(entry, dict) else entry
            value = self._clean_text(value)
            if value:
                values.append(value)
        return self._dedupe(values)

    @staticmethod
    def _normalize_date(value):
        if not value:
            return ""
        text = str(value)
        match = re.search(r"(\d{4})[-/](\d{2})[-/](\d{2})", text)
        if match:
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        match = re.search(r"(\d{4})[-/](\d{2})", text)
        if match:
            return f"{match.group(1)}-{match.group(2)}-01"
        match = re.search(r"\b(\d{4})\b", text)
        if match:
            return f"{match.group(1)}-01-01"
        return ""

    @staticmethod
    def _normalize_doi(value):
        if not value:
            return ""
        text = str(value).strip()
        text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.I)
        text = re.sub(r"^doi:\s*", "", text, flags=re.I)
        return text.strip()

    @staticmethod
    def _dedupe(items):
        seen = set()
        out = []
        for item in items:
            key = item.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    @staticmethod
    def _clean_text(value):
        if value is None:
            return ""
        text = str(value)
        text = unescape(text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"[*_`#>]+", " ", text)
        text = re.sub(r"\r\n|\r|\n|\t", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()
