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
    COMMUNITY_IDS = (COMMUNITY_ID, "00a6222f-4761-4167-81e1-44007f19557f")
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
        self._check_cancelled()
        if limit is not None and limit <= 0:
            return 0
        saved = 0
        seen_items = set()
        for scope in self.COMMUNITY_IDS:
            self._check_cancelled()
            if limit is not None and saved >= limit:
                break
            self._active_community = scope
            count, complete = self._crawl_community(None if limit is None else limit-saved, seen_items)
            saved += count
            if not complete:
                break
        self._check_cancelled()
        return saved

    def _crawl_community(self, limit, seen_items):
        saved = 0
        page = 0
        scope_items = set()
        expected_pages = expected_total = None
        while True:
            self._check_cancelled()
            if limit is not None and saved >= limit:
                return saved, False
            data = self._fetch_list(page)
            if not data:
                self._report_source_failure('fetch_failed')
                return saved, False
            try:
                result = data['_embedded']['searchResult']
                info = result['page']
                total_pages, total = info['totalPages'], info['totalElements']
                records = result.get('_embedded', {}).get('objects', [])
            except (KeyError, TypeError, AttributeError):
                self._report_source_failure('invalid_response')
                return saved, False
            if (type(total_pages) is not int or total_pages < 0 or
                    type(total) is not int or total < 0 or
                    type(info.get('number')) is not int or info['number'] != page or
                    not isinstance(records, list) or
                    (expected_pages is not None and total_pages != expected_pages) or
                    (expected_total is not None and total != expected_total)):
                self._report_source_failure('invalid_response')
                return saved, False
            expected_pages, expected_total = total_pages, total
            if not records:
                if total != len(scope_items):
                    self._report_source_failure('invalid_response')
                    return saved, False
                return saved, True
            if page >= total_pages:
                self._report_source_failure('invalid_response')
                return saved, False
            new_in_scope = 0
            for record in records:
                self._check_cancelled()
                if limit is not None and saved >= limit:
                    return saved, False
                try:
                    item = self._indexable_object(record)
                    uuid = item.get('uuid') or item.get('id')
                    if not isinstance(uuid, str) or not uuid:
                        raise RuntimeError('missing source identity')
                    if uuid in scope_items:
                        self._report_source_failure('invalid_response')
                        continue
                    scope_items.add(uuid)
                    new_in_scope += 1
                    if uuid in seen_items:
                        continue
                    seen_items.add(uuid)
                    # Search results include collection navigation entries as
                    # well as documents. They count toward source pagination.
                    if item.get('type') in ('collection', 'community'):
                        continue
                    time.sleep(self._delay)
                    self._check_cancelled()
                    detail = self._fetch_item(uuid)
                    if not detail or (detail.get('uuid') or detail.get('id')) != uuid:
                        raise RuntimeError('missing or mismatched item detail')
                    bitstreams = self._fetch_bitstreams(detail, uuid)
                    parsed = self._parse_item(detail, bitstreams)
                    if getattr(self, '_access_held', False):
                        return saved, False
                    if len(parsed['abstract']) < self.MIN_ABSTRACT_CHARS:
                        continue
                    paper = {
                        'id': None, 'site_id': self.site_id,
                        'external_id': parsed['external_id'], 'title': parsed['title'],
                        'authors': json.dumps(parsed['authors'], ensure_ascii=False),
                        'abstract': parsed['abstract'], 'category': parsed['category'],
                        'keywords': json.dumps(parsed['keywords'], ensure_ascii=False),
                        'published_date': parsed['published_date'],
                        'listed_date': parsed['listed_date'],
                        'url': parsed['url'], 'pdf_url': parsed['pdf_url'],
                        'doi': parsed['doi'], 'department': parsed['department'],
                        'metadata': json.dumps(parsed['metadata'], ensure_ascii=False),
                    }
                    self._save_paper(paper)
                    if self._last_save_created:
                        saved += 1
                except Exception as exc:
                    self._report_source_failure('item_failed')
                    print(f"[{self.site_id}] item failed: {exc}")
                if getattr(self, '_access_held', False):
                    return saved, False
            if new_in_scope == 0:
                self._report_source_failure('invalid_response')
                return saved, False
            if page == total_pages-1:
                if len(scope_items) != total:
                    self._report_source_failure('invalid_response')
                    return saved, False
                return saved, True
            page += 1

    def _fetch_list(self, page):
        params = {
            "scope": getattr(self, '_active_community', self.COMMUNITY_ID),
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
        if not bundles_data:
            raise RuntimeError('bundle metadata fetch failed')
        if 'bundles' not in (bundles_data.get('_embedded') or {}) and (bundles_data.get('page') or {}).get('totalElements') != 0:
            raise RuntimeError('invalid bundle metadata')
        if (bundles_data.get('page') or {}).get('totalPages', 1) > 1:
            raise RuntimeError('bundle pagination not fully resolved')
        bundles = bundles_data.get("_embedded", {}).get("bundles", []) if bundles_data else []
        if not isinstance(bundles, list):
            raise RuntimeError('invalid bundle metadata')

        ordered = sorted(bundles, key=lambda b: 0 if b.get("name") == "ORIGINAL" else 1)
        bitstreams = []
        for bundle in ordered:
            self._check_cancelled()
            if bundle.get('name') != 'ORIGINAL':
                continue
            href = bundle.get("_links", {}).get("bitstreams", {}).get("href")
            if not href:
                raise RuntimeError('missing original bitstream metadata link')
            data = self._curl_json(
                href,
                context=f"item {uuid} bundle {bundle.get('name') or bundle.get('uuid')} bitstreams",
            )
            if not data:
                raise RuntimeError('original bitstream metadata fetch failed')
            if (data.get('page') or {}).get('totalPages', 1) > 1:
                raise RuntimeError('original bitstream pagination not fully resolved')
            found = data.get("_embedded", {}).get("bitstreams", [] if (data.get('page') or {}).get('totalElements') == 0 else None)
            if not isinstance(found, list):
                raise RuntimeError('invalid original bitstream metadata')
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
            "-sSL",
            "--fail",
            "--write-out",
            "\n%{http_code}",
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
            self._check_cancelled()
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=55)
                self._check_cancelled()
                stdout = result.stdout or b""
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                body, separator, status_text = stdout.rpartition(b'\n')
                status = int(status_text) if separator and status_text.isdigit() else 0
                if status in (401,403,429):
                    self._access_held = True
                    self._report_source_failure('rate_limited' if status == 429 else 'authentication_failed')
                    return None
                if result.returncode == 0 and 200 <= status < 300 and body.strip():
                    return body.decode("utf-8", errors="replace")
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
                self._check_cancelled()
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
        # Issued and repository accession dates have different meanings.
        # Let the common persistence adapter classify the original precision.
        published_date = self._metadata_first(metadata, ('dc.date.issued',))
        listed_date = self._metadata_first(metadata, ('dc.date.accessioned',))
        url = self._item_url(item, metadata, uuid)
        doi = self._normalize_doi(
            self._metadata_first(metadata, ("dc.identifier.doi",))
            or self._doi_from_text(self._metadata_first(metadata, ("dc.identifier.citation",)))
        )
        department = self._metadata_first(metadata, ("dc.publisher",), preferred_languages=("en", None))
        pdf_url, file_summaries = self._select_file(bitstreams)

        metadata_summary = {
            "source_format": "json",
            "start_url": f"{self.base_url}/communities/{getattr(self, '_active_community', self.COMMUNITY_ID)}",
            "list_api": self.LIST_API_URL,
            "detail_api": self.ITEM_API_URL.format(uuid=uuid),
            "community_id": getattr(self, '_active_community', self.COMMUNITY_ID),
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
            "listed_date": listed_date,
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
