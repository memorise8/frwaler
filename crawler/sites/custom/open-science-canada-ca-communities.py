# -*- coding: utf-8 -*-
"""Crawler for the Open Science Canada community search."""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class OpenScienceCanadaCaCommunitiesCrawler(BaseCrawler):
    site_id = "open-science-canada-ca-communities"
    site_name = "Custom: open-science-canada-ca-communities"
    base_url = "https://open-science.canada.ca"

    COMMUNITY_ID = "307d8eca-9337-48d3-a296-6ea96f5a75c5"
    START_URL = (
        "https://open-science.canada.ca/communities/"
        "307d8eca-9337-48d3-a296-6ea96f5a75c5?page=1&spc.sf=score"
        "&spc.sd=DESC&scope=307d8eca-9337-48d3-a296-6ea96f5a75c5"
        "&spc.page=1&query=*"
    )
    LIST_API_URL = f"{base_url}/server/api/discover/search/objects"
    ITEM_API_URL = f"{base_url}/server/api/core/items/{{uuid}}"

    PAGE_SIZE = 25
    BACKOFF_SECONDS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 100

    def crawl(self, limit=None):
        saved = 0
        page = 0
        item_number = 0

        while True:
            if limit is not None and saved >= limit:
                break

            list_data = self._fetch_list(page)
            if not list_data:
                print(f"[{self.site_id}] list API failed at page {page + 1}; stopping")
                break

            records = self._list_records(list_data)
            if not records:
                print(f"[{self.site_id}] no records returned at page {page + 1}; stopping")
                break

            page_info = list_data.get("_embedded", {}).get("searchResult", {}).get("page", {})
            total = page_info.get("totalElements")
            print(
                f"[{self.site_id}] page {page + 1}: discovered {len(records)} records"
                + (f" of {total}" if total is not None else "")
            )

            for record in records:
                if limit is not None and saved >= limit:
                    break

                item_number += 1
                try:
                    time.sleep(self._delay)

                    list_item = self._indexable_object(record)
                    uuid = (list_item.get("uuid") or list_item.get("id") or "").strip()
                    if not uuid:
                        raise RuntimeError("list record has no item uuid")

                    detail = self._fetch_item(uuid)
                    if not detail:
                        raise RuntimeError("item detail API failed after retries")

                    bitstreams = self._fetch_bitstreams(detail, uuid)
                    parsed = self._parse_item(detail, bitstreams)

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
                        "external_id": parsed["external_id"],
                        "title": parsed["title"],
                        "authors": json.dumps(parsed["authors"], ensure_ascii=False),
                        "abstract": abstract,
                        "category": parsed["category"],
                        "keywords": json.dumps(parsed["keywords"], ensure_ascii=False),
                        "published_date": parsed["published_date"],
                        "url": parsed["url"],
                        "pdf_url": parsed["pdf_url"],
                        "doi": parsed["doi"],
                        "department": parsed["department"],
                        "metadata": json.dumps(parsed["metadata"], ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {parsed['title'][:90]}")
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_number} failed: {exc}")
                    continue

            page_meta = self._page_meta(list_data)
            total_pages = page_meta.get("totalPages")
            if total_pages is not None and page + 1 >= int(total_pages):
                break
            if len(records) < self.PAGE_SIZE:
                break
            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    def _fetch_list(self, page):
        params = {
            "scope": self.COMMUNITY_ID,
            "query": "*",
            "page": str(page),
            "size": str(self.PAGE_SIZE),
            "sort": "score,DESC",
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

    def _parse_item(self, item, bitstreams):
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        uuid = (item.get("uuid") or item.get("id") or "").strip()
        if not uuid:
            raise RuntimeError("detail record has no uuid")

        title = self._metadata_first(metadata, ("dc.title",), preferred_languages=("en",))
        title = title or self._clean_text(item.get("name") or "")
        if not title:
            raise RuntimeError(f"{uuid} has no title")

        abstract = self._metadata_first(
            metadata,
            ("dc.description.abstract", "dcterms.abstract", "dc.description"),
            preferred_languages=("en", None),
        )
        if len(abstract) < 50:
            translated = self._metadata_first(
                metadata,
                ("dc.description.abstract-fosrctranslation",),
                preferred_languages=("en", "fr", None),
            )
            if translated:
                abstract = translated
        if len(abstract) < 50:
            html_url = self._item_url(item, metadata, uuid)
            html = self._curl_get(
                html_url,
                context=f"item {uuid} html fallback",
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            )
            if html:
                soup = self._make_soup(html)
                if soup is not None:
                    fallback = self._parse_html_abstract(soup)
                    if fallback:
                        abstract = fallback
        abstract = self._clean_text(abstract)

        authors = self._metadata_values(
            metadata,
            ("dc.contributor.author", "dc.creator", "dc.contributor"),
        )
        keywords = self._dedupe(
            self._metadata_values(metadata, ("creativework.keywords",))
            + self._metadata_values(metadata, ("dc.subject.en",), preferred_languages=("en", None))
            + self._metadata_values(metadata, ("dc.subject",), preferred_languages=("en", None))
        )
        category = (
            self._metadata_first(metadata, ("dc.subject.en",), preferred_languages=("en", None))
            or self._metadata_first(metadata, ("dc.subject",), preferred_languages=("en", None))
            or self._metadata_first(metadata, ("dc.type",), preferred_languages=("en", None))
        )
        published_date = self._normalize_date(
            self._metadata_first(
                metadata,
                (
                    "dc.date.issued",
                    "dc.date.accepted",
                    "dc.date.available",
                    "dc.date.accessioned",
                    "dc.date.submitted",
                ),
            )
        )
        url = self._item_url(item, metadata, uuid)
        doi = self._normalize_doi(
            self._metadata_first(metadata, ("dc.identifier.doi",))
            or self._doi_from_text(self._metadata_first(metadata, ("dc.identifier.citation",)))
        )
        department = self._metadata_first(metadata, ("dc.publisher",), preferred_languages=("en", None))
        pdf_url, file_summaries = self._select_file(bitstreams)

        metadata_summary = {
            "source_format": "json",
            "start_url": self.START_URL,
            "list_api": self.LIST_API_URL,
            "detail_api": self.ITEM_API_URL.format(uuid=uuid),
            "community_id": self.COMMUNITY_ID,
            "uuid": uuid,
            "handle": item.get("handle"),
            "name": item.get("name"),
            "type": item.get("type"),
            "doi": doi,
            "journal": self._metadata_first(metadata, ("local.article.journaltitle",)),
            "citation": self._metadata_first(metadata, ("dc.identifier.citation",)),
            "license": self._metadata_first(metadata, ("dc.rights",), preferred_languages=("en", None)),
            "rights_uri": self._metadata_first(metadata, ("dc.rights.uri",), preferred_languages=("en", None)),
            "language": self._metadata_first(metadata, ("dc.language.iso",)),
            "document_type": self._metadata_first(metadata, ("dc.type",), preferred_languages=("en", None)),
            "bitstreams": file_summaries,
            "metadata_keys": sorted(metadata.keys()),
        }

        return {
            "external_id": uuid,
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "category": category,
            "keywords": keywords,
            "published_date": published_date,
            "url": url,
            "pdf_url": pdf_url,
            "doi": doi,
            "department": department,
            "metadata": metadata_summary,
        }

    def _select_file(self, bitstreams):
        summaries = []
        for bitstream in bitstreams:
            name = self._clean_text(bitstream.get("name") or "")
            content_url = bitstream.get("_links", {}).get("content", {}).get("href") or ""
            summary = {
                "uuid": bitstream.get("uuid") or bitstream.get("id"),
                "name": name,
                "bundleName": bitstream.get("bundleName"),
                "sizeBytes": bitstream.get("sizeBytes"),
                "content_url": content_url,
                "checksum": (bitstream.get("checkSum") or {}).get("value"),
            }
            if name or content_url:
                summaries.append(summary)

        for summary in summaries:
            if summary["name"].lower().endswith(".pdf"):
                return summary["content_url"], summaries
        for summary in summaries:
            if summary.get("bundleName") == "ORIGINAL" and summary["content_url"]:
                return summary["content_url"], summaries
        for summary in summaries:
            if summary["content_url"]:
                return summary["content_url"], summaries
        return "", summaries

    def _item_url(self, item, metadata, uuid):
        uri = self._metadata_first(metadata, ("dc.identifier.uri",))
        if uri.startswith("http"):
            return uri
        handle = item.get("handle")
        if handle:
            return f"{self.base_url}/handle/{handle}"
        return f"{self.base_url}/items/{uuid}"

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

    def _parse_html_abstract(self, soup):
        for name in ("description", "og:description", "dcterms.abstract"):
            value = self._meta_content(soup, name)
            if value:
                return value
        main = soup.find("main") or soup
        heading = main.find(string=re.compile(r"abstract|description|resume", re.I))
        if heading:
            parent = heading.find_parent()
            block = parent.find_next(["p", "div", "dd"]) if parent else None
            return self._clean_text(block)
        return ""

    def _meta_content(self, soup, name):
        tag = (
            soup.find("meta", attrs={"property": name})
            or soup.find("meta", attrs={"name": name})
        )
        if tag and tag.get("content"):
            return self._clean_text(tag.get("content"))
        return ""

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
    def _doi_from_text(value):
        if not value:
            return ""
        match = re.search(r"\b10\.\d{4,9}/[^\s,;]+", str(value), flags=re.I)
        return match.group(0).rstrip(".") if match else ""

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
