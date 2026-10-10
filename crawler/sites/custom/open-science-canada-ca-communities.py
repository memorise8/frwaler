# -*- coding: utf-8 -*-
"""Crawler for the Open Science Canada community search."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import tempfile
from pathlib import Path
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
    DELIVERY_CURSOR_VERSION = 1
    BACKFILL_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

    def crawl(self, limit=None):
        self._check_cancelled()
        if limit is not None and limit <= 0:
            return 0
        if self.delivery_mode == 'backfill':
            return self._crawl_backfill(limit)
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
        else:
            if not self.delivery_source_failure:
                self._mark_exhausted()
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
                    detail = item if self._complete_inline_item(item, uuid) else self._fetch_item(uuid)
                    if not detail or (detail.get('uuid') or detail.get('id')) != uuid:
                        raise RuntimeError('missing or mismatched item detail')
                    bitstreams = self._fetch_bitstreams(detail, uuid)
                    parsed = self._parse_item(detail, bitstreams)
                    if getattr(self, '_access_held', False):
                        return saved, False
                    if len(parsed['abstract']) < self.MIN_ABSTRACT_CHARS:
                        # A catalogue entry without a usable body is an unresolved
                        # collection gap, not successful source exhaustion.
                        self._report_source_failure('item_failed')
                        continue
                    self._save_parsed(parsed)
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

    def _save_parsed(self, parsed):
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

    @staticmethod
    def _cursor_hash(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                          separators=(',', ':')).encode()).hexdigest()

    def _cursor_contract(self):
        return self._cursor_hash([self.COMMUNITY_IDS, self.PAGE_SIZE, '*', 'score,DESC',
                                  self.DELIVERY_CURSOR_VERSION])

    def _validate_cursor(self, cursor):
        if (not isinstance(cursor, dict) or type(cursor.get('version')) is not int
                or cursor['version'] != self.DELIVERY_CURSOR_VERSION
                or cursor.get('contract') != self._cursor_contract()
                or cursor.get('scope') not in self.COMMUNITY_IDS):
            raise ValueError('invalid source cursor contract')
        for key in ('page', 'offset', 'page_items', 'total', 'total_pages'):
            if type(cursor.get(key)) is not int or cursor[key] < 0:
                raise ValueError('invalid source cursor range')
        if (cursor['total_pages'] != (cursor['total'] + self.PAGE_SIZE - 1) // self.PAGE_SIZE
                or cursor['page'] > max(cursor['total_pages'] - 1, 0)
                or cursor['page_items'] != min(self.PAGE_SIZE, cursor['total'] - cursor['page'] * self.PAGE_SIZE)
                or cursor['offset'] > cursor['page_items']):
            raise ValueError('inconsistent source cursor position')
        for key in ('page_hash', 'prefix_hash'):
            if not isinstance(cursor.get(key), str) or not re.fullmatch('[0-9a-f]{64}', cursor[key]):
                raise ValueError('invalid source cursor anchor')

    def _backfill_page(self, scope, page):
        self._active_community = scope
        raw = self._fetch_list(page)
        if not raw:
            raise ConnectionError('source list unavailable')
        result = raw['_embedded']['searchResult']
        info = result['page']
        records = result.get('_embedded', {}).get('objects', [])
        if (any(type(info.get(k)) is not int for k in ('number', 'size', 'totalPages', 'totalElements'))
                or info['number'] != page or info['size'] != self.PAGE_SIZE
                or info['totalElements'] < 0
                or info['totalPages'] != (info['totalElements'] + self.PAGE_SIZE - 1) // self.PAGE_SIZE
                or page > max(info['totalPages'] - 1, 0) or not isinstance(records, list)
                or len(records) != min(self.PAGE_SIZE, info['totalElements'] - page * self.PAGE_SIZE)):
            raise ValueError('incomplete source pagination')
        items = [self._indexable_object(r) for r in records]
        ids = [i.get('uuid') for i in items]
        if (any(not isinstance(uid, str) or not re.fullmatch('[0-9a-fA-F-]{36}', uid) for uid in ids)
                or len(set(ids)) != len(ids)
                or any(i.get('type') not in ('item', 'collection', 'community') for i in items)):
            raise ValueError('invalid or repeated source identity')
        return dict(items=items, total=info['totalElements'], total_pages=info['totalPages'],
                    page_hash=self._cursor_hash(items))

    def _stored_cursor_item(self, item):
        """No merging or rewriting when stored identities disagree with the source."""
        postgres = os.environ.get('LIBERTREE_DB_BACKEND', 'sqlite').lower() == 'postgres'
        if not postgres and not self._conn_is_libertree():
            raise ValueError('backfill requires the delivery document database')
        uid = item['uuid']
        url = self._item_url(item, item.get('metadata') or {}, uid)
        mark = '%s' if postgres else '?'
        rows = self._conn.execute(f'SELECT post_number,meta_url FROM documents WHERE site_id={mark} '
                                  f'AND (post_number={mark} OR meta_url={mark})',
                                  (self.site_id, uid, url)).fetchall()
        if len(rows) > 1:
            raise ValueError('ambiguous stored source identity')
        for row in rows:
            post, stored_url = (row['post_number'], row['meta_url']) if hasattr(row, 'keys') else row
            if post != uid or stored_url != url:
                raise ValueError('stored source identity requires review')
        return bool(rows)

    def _backfill_checkpoint(self, scope, page, offset, data, prefix, done=0):
        self._advance_cursor(dict(version=self.DELIVERY_CURSOR_VERSION, contract=self._cursor_contract(),
                                  scope=scope, page=page, offset=offset, page_items=len(data['items']),
                                  total=data['total'], total_pages=data['total_pages'],
                                  page_hash=data['page_hash'], prefix_hash=prefix), items_done=done)

    def _crawl_backfill(self, limit):
        self._source_failure, self._exhausted, self._pending_cursor, self._cursor_items_done = None, False, None, 0
        resume = self.delivery_cursor
        if resume is not None:
            try:
                self._validate_cursor(resume)
            except Exception:
                self._report_source_failure('configuration')
                return 0
        started, saved = time.monotonic(), 0
        prefix = self._cursor_hash(['source-prefix', self._cursor_contract()])
        seen = {}
        matched = resume is None

        def budget():
            if time.monotonic() - started < self.BACKFILL_WALL_SECONDS:
                return False
            if self.delivery_cursor_items_done == 0:
                self._report_source_failure('invalid_response')
            return True

        for scope in self.COMMUNITY_IDS:
            page, expected_bounds, scope_ids = 0, None, set()
            while True:
                self._check_cancelled()
                if (matched and limit is not None and saved >= limit) or budget():
                    return saved
                try:
                    data = self._backfill_page(scope, page)
                    bounds = data['total'], data['total_pages']
                    if expected_bounds is not None and expected_bounds != bounds:
                        raise ValueError('source scope bounds changed')
                    expected_bounds = bounds
                    ids = {i['uuid'] for i in data['items']}
                    if scope_ids & ids:
                        raise ValueError('source page repeats earlier identities')
                    scope_ids.update(ids)
                    at_resume = bool(resume and scope == resume['scope'] and page == resume['page'])
                    if at_resume:
                        if (prefix != resume['prefix_hash'] or data['page_hash'] != resume['page_hash']
                                or bounds != (resume['total'], resume['total_pages'])
                                or len(data['items']) != resume['page_items']):
                            raise ValueError('source prefix or resume page changed')
                        matched = True
                    offset = resume['offset'] if at_resume else 0
                    # Recheck the complete source prefix and its persisted rows.
                    # This catches equal-count changes even in earlier scopes.
                    for index, item in enumerate(data['items']):
                        if matched and index >= offset:
                            break
                        uid = item['uuid']
                        if item['type'] == 'item' and not self._stored_cursor_item(item):
                            raise ValueError('cursor references a missing persisted document')
                        fingerprint = self._cursor_hash(item)
                        if uid in seen and seen[uid] != fingerprint:
                            raise ValueError('cross-scope source identity changed')
                        seen[uid] = fingerprint
                    if matched:
                        self._backfill_checkpoint(scope, page, offset, data, prefix)
                except Exception as exc:
                    self._report_source_failure('fetch_failed' if isinstance(exc, ConnectionError) else 'invalid_response')
                    return saved
                if matched:
                    for index in range(offset, len(data['items'])):
                        self._check_cancelled()
                        if (limit is not None and saved >= limit) or budget():
                            return saved
                        item = data['items'][index]
                        uid, fingerprint = item['uuid'], self._cursor_hash(item)
                        try:
                            if uid in seen and seen[uid] != fingerprint:
                                raise ValueError('cross-scope source identity changed')
                            if item['type'] == 'item' and not self._stored_cursor_item(item):
                                time.sleep(self._delay)
                                self._check_cancelled()
                                detail = item if self._complete_inline_item(item, uid) else self._fetch_item(uid)
                                if not detail or (detail.get('uuid') or detail.get('id')) != uid:
                                    raise ValueError('missing or mismatched item detail')
                                parsed = self._parse_item(detail, self._fetch_bitstreams(detail, uid))
                                if getattr(self, '_access_held', False):
                                    return saved
                                if len(parsed['abstract']) < self.MIN_ABSTRACT_CHARS:
                                    raise ValueError('unresolved source body gap')
                                self._save_parsed(parsed)
                                if self._last_save_created:
                                    saved += 1
                            seen[uid] = fingerprint
                            self._backfill_checkpoint(scope, page, index + 1, data, prefix, done=1)
                            self._check_cancelled()
                        except Exception:
                            self._report_source_failure('item_failed')
                            return saved
                prefix = self._cursor_hash([prefix, scope, page, data['page_hash'], *bounds])
                if page == max(data['total_pages'] - 1, 0):
                    break
                page += 1
        self._check_cancelled()
        if not matched:
            self._report_source_failure('invalid_response')
        elif not self.delivery_source_failure:
            self._mark_exhausted()
        return saved

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

    def _complete_inline_item(self, item, uuid):
        """DSpace embeds full Item resources in search results; summaries need GET."""
        expected = self.ITEM_API_URL.format(uuid=uuid)
        links = item.get('_links') or {}
        return (item.get('type') == 'item' and item.get('uuid') == uuid
                and item.get('id') == uuid
                and all(key in item for key in ('name', 'handle', 'metadata'))
                and isinstance(item['metadata'], dict)
                and all(isinstance(values, list) and all(isinstance(v, dict) and
                        (v.get('value') is None or isinstance(v.get('value'), str)) for v in values)
                        for values in item['metadata'].values())
                and all(type(item.get(flag)) is bool for flag in ('inArchive', 'discoverable', 'withdrawn'))
                and (links.get('self') or {}).get('href') == expected
                and (links.get('bundles') or {}).get('href') == expected + '/bundles')

    @staticmethod
    def _complete_embedded_page(data, key, expected_url):
        if not isinstance(data, dict):
            raise RuntimeError('missing embedded metadata page')
        info = data.get('page') or {}
        records = (data.get('_embedded') or {}).get(key, [])
        if (any(type(info.get(k)) is not int for k in ('number', 'size', 'totalPages', 'totalElements'))
                or info['number'] != 0 or info['size'] <= 0 or info['totalElements'] < 0
                or info['totalPages'] != (info['totalElements'] + info['size'] - 1) // info['size']
                or info['totalPages'] > 1 or not isinstance(records, list)
                or len(records) != info['totalElements']
                or any(not isinstance(r, dict) for r in records)
                or (data.get('_links', {}).get('self') or {}).get('href') != expected_url):
            raise RuntimeError('incomplete or mismatched embedded metadata page')
        return records

    def _fetch_bitstreams(self, item, uuid):
        expected = self.ITEM_API_URL.format(uuid=uuid) + '/bundles'
        bundles_url = (item.get('_links', {}).get('bundles') or {}).get('href', expected)
        if bundles_url != expected:
            raise RuntimeError('unreviewed item bundle destination')
        url = bundles_url + '?embed=bitstreams'
        data = self._curl_json(url, context=f'item {uuid} embedded bundles')
        bundles = self._complete_embedded_page(data, 'bundles', url)
        ids = [b.get('uuid') for b in bundles]
        if any(not isinstance(uid, str) or not uid for uid in ids) or len(ids) != len(set(ids)):
            raise RuntimeError('missing or duplicate source bundle identity')
        bitstreams, seen = [], set()
        for bundle in bundles:
            self._check_cancelled()
            if bundle.get('type') != 'bundle':
                raise RuntimeError('invalid source bundle type')
            if bundle.get('name') != 'ORIGINAL':
                continue
            href = f"{self.base_url}/server/api/core/bundles/{bundle['uuid']}/bitstreams"
            if (bundle.get('_links', {}).get('bitstreams') or {}).get('href') != href:
                raise RuntimeError('unreviewed original bitstream destination')
            tree = (bundle.get('_embedded') or {}).get('bitstreams')
            for bit in self._complete_embedded_page(tree, 'bitstreams', href):
                uid = bit.get('uuid')
                checksum = bit.get('checkSum') or {}
                if (not isinstance(uid, str) or not re.fullmatch(r'[0-9a-fA-F-]{36}', uid)
                        or uid in seen or bit.get('type') != 'bitstream'
                        or bit.get('bundleName') != 'ORIGINAL'
                        or not isinstance(bit.get('name'), str) or not bit['name'].strip()
                        or type(bit.get('sizeBytes')) is not int or bit['sizeBytes'] < 0
                        or checksum.get('checkSumAlgorithm') != 'MD5'
                        or not re.fullmatch(r'[0-9a-fA-F]{32}', str(checksum.get('value', '')))
                        or (bit.get('_links', {}).get('content') or {}).get('href') !=
                            f'{self.base_url}/server/api/core/bitstreams/{uid}/content'):
                    raise RuntimeError('invalid or duplicate original bitstream identity')
                seen.add(uid)
                bitstreams.append(bit)
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
        pdf_url, file_summaries = self._select_file(bitstreams)
        pdf_provenance = {}
        if len(abstract) < self.MIN_ABSTRACT_CHARS and pdf_url:
            abstract, pdf_provenance = self._abstract_from_pdf(pdf_url, bitstreams)

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
            **pdf_provenance,
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

    def _abstract_from_pdf(self, pdf_url, bitstreams):
        """Use only this item's verified ORIGINAL PDF; do not publish file flags."""
        from delivery.files.worker import run_stage

        matches = [b for b in bitstreams if (b.get('_links', {}).get('content') or {}).get('href') == pdf_url]
        if len(matches) != 1:
            raise RuntimeError('ambiguous source PDF reference')
        bit = matches[0]
        uid = bit.get('uuid', '')
        checksum = bit.get('checkSum') or {}
        expected_size = bit.get('sizeBytes')
        if (not isinstance(uid, str) or not re.fullmatch(r'[0-9a-fA-F-]{36}', uid)
                or pdf_url != f'{self.base_url}/server/api/core/bitstreams/{uid}/content'
                or bit.get('bundleName') != 'ORIGINAL'
                or not str(bit.get('name', '')).lower().endswith('.pdf')
                or type(expected_size) is not int or not 0 < expected_size <= 100 * 1024**2
                or checksum.get('checkSumAlgorithm') != 'MD5'
                or not re.fullmatch(r'[0-9a-fA-F]{32}', str(checksum.get('value', '')))):
            raise RuntimeError('unreviewed source PDF identity, size or checksum')
        self._check_cancelled()
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix='open-science-pdf-') as directory:
            root = Path(directory)
            md5, digest, size = hashlib.md5(), hashlib.sha256(), 0
            with self._session.get(pdf_url, stream=True, timeout=(10, 45), allow_redirects=False) as response:
                if response.status_code in (401, 403, 429):
                    self._access_held = True
                    self._report_source_failure('rate_limited' if response.status_code == 429 else 'authentication_failed')
                    raise RuntimeError('source PDF access held')
                if response.status_code != 200:
                    raise RuntimeError('source PDF response requires review')
                if int(response.headers.get('Content-Length', '0')) > expected_size:
                    raise RuntimeError('source PDF exceeds declared size')
                with (root / 'source.pdf').open('xb') as handle:
                    for chunk in response.iter_content(65536):
                        self._check_cancelled()
                        size += len(chunk)
                        if size > expected_size or time.monotonic() - started > 180:
                            raise RuntimeError('source PDF download budget exceeded')
                        md5.update(chunk)
                        digest.update(chunk)
                        handle.write(chunk)
            if size != expected_size or md5.hexdigest() != checksum['value'].lower():
                raise RuntimeError('source PDF size or checksum mismatch')
            with (root / 'source.pdf').open('rb') as handle:
                if handle.read(5) != b'%PDF-':
                    raise RuntimeError('source response is not PDF')
            self._check_cancelled()
            result = run_stage('text', root, timeout=120)
            self._check_cancelled()
            if not result.get('success'):
                raise RuntimeError('source PDF text unavailable: ' + str(result.get('error', 'unknown')))
            with (root / 'output.txt').open(encoding='utf-8') as handle:
                excerpt = ' '.join(handle.read(12000).split())[:3000]
            with (root / 'output.txt').open('rb') as handle:
                text_hash = hashlib.file_digest(handle, 'sha256').hexdigest()
        if len(excerpt) < self.MIN_ABSTRACT_CHARS:
            raise RuntimeError('source PDF excerpt below body threshold')
        return excerpt, dict(abstract_source='repository_pdf_text_excerpt', abstract_pdf_url=pdf_url,
                             abstract_pdf_sha256=digest.hexdigest(), abstract_pdf_md5=md5.hexdigest(),
                             abstract_pdf_text_sha256=text_hash, abstract_pdf_text_characters=result.get('chars'))

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
            if summary["name"].lower().endswith(".pdf") and summary["content_url"]:
                return summary["content_url"], summaries
        # ORIGINAL also contains DOCX files. Keep their references in the
        # attachment summary; the PDF worker must not receive a DOCX as pdf_url.
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
