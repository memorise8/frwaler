# -*- coding: utf-8 -*-
"""Crawler for the ANU Open Research Repository — general search listing."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class OpenresearchRepositoryAnuEduAuSearchCrawler(BaseCrawler):
    site_id = "openresearch-repository-anu-edu-au-search"
    site_name = "Custom: openresearch-repository-anu-edu-au-search"
    base_url = "https://openresearch-repository.anu.edu.au"
    DELIVERY_ORDER = "newest_first"

    API_BASE = "https://openresearch-repository.anu.edu.au/server/api"
    PAGE_SIZE = 20
    BACKOFF_SECONDS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 50
    SAFETY_PAGE_CAP = 200
    WALL_CLOCK_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.monotonic()
        saved = 0
        page = (self.delivery_cursor or {}).get("page", 0)
        seen_urls = set()

        try:
            while True:
                if limit is not None and saved >= limit:
                    break

                elapsed = time.monotonic() - start_time
                if elapsed > self.WALL_CLOCK_BUDGET_SECONDS:
                    print(f"[{self.site_id}] wall-clock budget of {self.WALL_CLOCK_BUDGET_SECONDS}s "
                          f"exceeded at page {page}; stopping cleanly")
                    break

                if page >= self.SAFETY_PAGE_CAP:
                    print(f"[{self.site_id}] safety cap of {self.SAFETY_PAGE_CAP} pages reached; stopping")
                    break

                list_url = self._list_url(page)
                data = self._fetch_json(list_url, context=f"list page {page}")
                if data is None:
                    print(f"[{self.site_id}] list endpoint failed at page {page}; stopping")
                    break

                objects = self._extract_objects(data)
                if not objects:
                    print(f"[{self.site_id}] no records found at page {page}; stopping")
                    break

                new_count = 0
                for idx, obj in enumerate(objects, start=1):
                    if limit is not None and saved >= limit:
                        break

                    item_label = f"{page}.{idx}"
                    try:
                        indexable = (obj.get("_embedded") or {}).get("indexableObject") or {}
                        uuid = indexable.get("uuid") or indexable.get("id")
                        if not uuid:
                            raise RuntimeError("record has no item uuid")

                        detail_url = self._handle_url(indexable) or f"{self.base_url}/items/{uuid}"
                        if detail_url in seen_urls:
                            continue
                        seen_urls.add(detail_url)
                        new_count += 1

                        time.sleep(self.detail_delay)
                        parsed = self._fetch_and_parse_item(uuid, indexable, detail_url, item_label)
                        if parsed is None:
                            continue

                        abstract = parsed.get("abstract") or ""
                        if len(abstract) < self.MIN_ABSTRACT_CHARS:
                            print(f"[{self.site_id}] item {item_label} skipped: "
                                  f"abstract too short ({len(abstract)} chars)")
                            continue

                        paper = {
                            "id": None,
                            "site_id": self.site_id,
                            "external_id": parsed["external_id"],
                            "post_number": parsed["post_number"],
                            "title": parsed["title"],
                            "abstract": abstract,
                            "published_date": parsed["published_date"],
                            "posted_date": parsed["listed_date"],
                            "authors": parsed["authors"],
                            "publisher": parsed["publisher"],
                            "department": parsed["department"],
                            "journal": parsed["journal"],
                            "url": parsed["url"],
                            "pdf_url": parsed["pdf_url"],
                            "keywords": parsed["keywords"],
                            "category": parsed["category"],
                            "doi": parsed["doi"],
                            "original_filename": parsed["original_filename"],
                            "metadata": json.dumps(parsed["metadata"], ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        counter = f"{saved}/{limit}" if limit else str(saved)
                        print(f"[{self.site_id}] saved {counter}: {parsed['title'][:90]}")
                    except Exception as exc:
                        print(f"[{self.site_id}] item {item_label} failed: {exc}")
                        continue

                self._advance_cursor({"page": page + 1}, items_done=len(objects))

                if new_count == 0:
                    print(f"[{self.site_id}] page {page}: all {len(objects)} records already seen; stopping")
                    break

                if page % 10 == 0:
                    limit_label = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

                if limit is not None and saved >= limit:
                    break
                page += 1
        except KeyboardInterrupt:
            print(f"[{self.site_id}] interrupted by user. Saved {saved} so far.")
            raise

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, context="request"):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--max-time",
            "45",
            "--connect-timeout",
            "15",
            "-A",
            self.USER_AGENT,
            "-H",
            "Accept: application/json,text/plain,*/*;q=0.8",
            "-H",
            "Accept-Language: en-AU,en;q=0.9",
            url,
        ]

        last_error = ""
        for attempt in range(1, 4):
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

            print(f"[{self.site_id}] {context} curl failed (attempt {attempt}/3): {last_error}")
            if attempt < 3:
                wait = self.BACKOFF_SECONDS[attempt - 1]
                print(f"[{self.site_id}] retrying in {wait}s...")
                time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None

    def _fetch_json(self, url, context):
        raw = self._curl_get(url, context=context)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] {context} returned invalid JSON: {exc}")
            return None

    # ------------------------------------------------------------------
    # List / item fetching
    # ------------------------------------------------------------------

    def _list_url(self, page):
        return (
            f"{self.API_BASE}/discover/search/objects"
            f"?query=&dsoType=item"
            f"&sort=dc.date.accessioned,DESC"
            f"&page={page}&size={self.PAGE_SIZE}"
        )

    @staticmethod
    def _extract_objects(data):
        try:
            return (
                (data.get("_embedded") or {})
                .get("searchResult", {})
                .get("_embedded", {})
                .get("objects", [])
            ) or []
        except AttributeError:
            return []

    def _handle_url(self, indexable):
        handle = indexable.get("handle")
        if handle:
            return f"{self.base_url}/handle/{handle}"
        return ""

    def _fetch_and_parse_item(self, uuid, list_indexable, detail_url, item_label):
        item_url = (
            f"{self.API_BASE}/core/items/{uuid}"
            "?embed=owningCollection/parentCommunity,bundles/bitstreams"
        )
        data = self._fetch_json(item_url, context=f"item {item_label} detail")
        indexable = data if isinstance(data, dict) and data.get("metadata") else list_indexable
        if not indexable or not indexable.get("metadata"):
            raise RuntimeError("item has no metadata")

        return self._parse_item(uuid, indexable, data, detail_url)

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_item(self, uuid, indexable, full_data, detail_url):
        meta = indexable.get("metadata") or {}

        title = self._first_meta(meta, "dc.title")
        title = self._one_line(title)
        if not title:
            raise RuntimeError("item has no title")

        handle = indexable.get("handle") or ""
        if handle:
            detail_url = f"{self.base_url}/handle/{handle}"
        else:
            detail_url = detail_url or f"{self.base_url}/items/{uuid}"

        abstract_values = self._all_meta(meta, "dc.description.abstract")
        abstract = "\n\n".join(abstract_values)
        abstract = self._clean_text(abstract)

        author_pairs = self._author_affiliation_pairs(meta)
        authors = self._dedupe(
            [self._format_author_name(name) for name, _aff in author_pairs if name]
        )

        keywords = self._all_meta(meta, "dc.subject")
        keywords = self._dedupe([self._one_line(k) for k in keywords if self._one_line(k)])

        category = self._first_meta(meta, "dc.type") or None

        raw_issued = self._first_meta(meta, "dc.date.issued")
        published_date = self._normalize_date(raw_issued)

        raw_accessioned = self._first_meta(meta, "dc.date.accessioned")
        listed_date = raw_accessioned[:10] if len(raw_accessioned) >= 10 else None

        # Journal / venue — dc.source for journal articles, dc.relation.ispartof
        # for conference proceedings / edited books.
        journal = self._first_meta(meta, "dc.source", "dc.relation.ispartof") or None
        series = self._first_meta(meta, "dc.relation.ispartofseries") or None
        volume = self._first_meta(meta, "local.identifier.citationvolume") or None
        issue = self._first_meta(meta, "local.identifier.citationissue") or None

        doi = self._extract_doi(meta)

        # Bundles / bitstreams (PDF discovery) — prefer freshly embedded detail
        # payload; fall back to a dedicated bundles fetch if embed was dropped.
        source = full_data if isinstance(full_data, dict) and full_data.get("metadata") else None
        bundles = self._bundles_from_embedded(source) if source else None
        if bundles is None:
            bundles = self._fetch_bundles(uuid)
        pdf_url, original_filename, bitstream_records = self._pdf_from_bundles(bundles)

        # Collection / community (department context)
        owning_collection = {}
        parent_community = {}
        if source:
            oc = (source.get("_embedded") or {}).get("owningCollection") or {}
            if oc:
                owning_collection = oc
                parent_community = (oc.get("_embedded") or {}).get("parentCommunity") or {}
        collection_name = self._one_line(self._first_meta(owning_collection.get("metadata") or {}, "dc.title"))
        community_name = self._one_line(self._first_meta(parent_community.get("metadata") or {}, "dc.title"))

        affiliations = self._dedupe([aff for _name, aff in author_pairs if aff])
        department = "; ".join(affiliations) if affiliations else None

        publisher = self._first_meta(meta, "dc.publisher") or "Australian National University"

        post_number = None
        if handle and "/" in handle:
            candidate = handle.rsplit("/", 1)[-1]
            if candidate.isdigit():
                post_number = candidate
        if not post_number:
            post_number = uuid

        metadata_raw = {
            "posted_date": raw_accessioned or None,
            "originalFilename": original_filename,
            "journal_raw": journal or None,
            "series": series,
            "volume": volume,
            "issue": issue,
            "item_uuid": uuid,
            "handle": handle or None,
            "dc_type": category or None,
            "language": self._first_meta(meta, "dc.language.iso") or None,
            "description_uri": self._first_meta(meta, "dc.identifier.uri") or None,
            "date_available": self._first_meta(meta, "dc.date.available") or None,
            "collection": collection_name or None,
            "community": community_name or None,
            "issn": self._first_meta(meta, "dc.identifier.issn") or None,
            "isbn": self._first_meta(meta, "dc.identifier.isbn") or None,
            "scopus_id": self._first_meta(meta, "dc.identifier.scopus") or None,
            "wos_id": self._first_meta(meta, "dc.identifier.other") or None,
            "pure_id": self._first_meta(meta, "local.identifier.pure") or None,
            "status": self._first_meta(meta, "dc.description.status") or None,
            "sponsorship": self._first_meta(meta, "dc.description.sponsorship") or None,
            "startpage": self._first_meta(meta, "local.bibliographicCitation.startpage") or None,
            "lastpage": self._first_meta(meta, "local.bibliographicCitation.lastpage") or None,
            "extent": self._first_meta(meta, "dc.format.extent") or None,
            "rights": self._first_meta(meta, "dc.rights") or None,
            "coverage_spatial": self._first_meta(meta, "dc.coverage.spatial") or None,
            "bitstreams": bitstream_records or None,
        }
        metadata_raw = {k: v for k, v in metadata_raw.items() if v not in (None, "", [])}

        return {
            "external_id": post_number,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "authors": "; ".join(authors) if authors else None,
            "publisher": publisher,
            "department": department,
            "journal": journal,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": ", ".join(keywords) if keywords else None,
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": metadata_raw,
        }

    def _author_affiliation_pairs(self, meta):
        """Return list of (author_name, affiliation) pairs.

        DSpace stores affiliation as ``local.contributor.affiliation`` with
        values like ``"Family, Given; Institution, ..."`` — split on the
        first ``;`` to separate the author name (already covered by
        ``dc.contributor.author``) from the institution text.
        """
        raw_authors = self._all_meta(meta, "dc.contributor.author")
        raw_affils = self._all_meta(meta, "local.contributor.affiliation")

        affil_by_name = {}
        for raw in raw_affils:
            name, _sep, rest = raw.partition(";")
            name = name.strip()
            rest = rest.strip()
            if name and rest:
                affil_by_name[name] = rest

        pairs = []
        for name in raw_authors:
            name = name.strip(" ;")
            if not name:
                continue
            pairs.append((name, affil_by_name.get(name, "")))
        return pairs

    @staticmethod
    def _extract_doi(meta):
        for key in ("local.identifier.doi", "dc.identifier.doi"):
            values = meta.get(key) or []
            for item in values:
                value = (item.get("value") or "").strip()
                if not value:
                    continue
                value = re.sub(r"^https?://(dx\.)?doi\.org/", "", value, flags=re.IGNORECASE)
                if value:
                    return value
        for item in meta.get("dc.identifier.uri") or []:
            value = (item.get("value") or "").strip()
            if "doi.org/" in value.lower():
                return re.sub(r"^https?://(dx\.)?doi\.org/", "", value, flags=re.IGNORECASE)
        return None

    # ------------------------------------------------------------------
    # Bundle / bitstream helpers
    # ------------------------------------------------------------------

    def _bundles_from_embedded(self, item_data):
        bundles_wrapper = (item_data.get("_embedded") or {}).get("bundles")
        if not isinstance(bundles_wrapper, dict):
            return None
        return (bundles_wrapper.get("_embedded") or {}).get("bundles")

    def _fetch_bundles(self, uuid):
        url = f"{self.API_BASE}/core/items/{uuid}/bundles?embed=bitstreams"
        data = self._fetch_json(url, context=f"item {uuid} bundles")
        if not isinstance(data, dict):
            return []
        return (data.get("_embedded") or {}).get("bundles") or []

    def _pdf_from_bundles(self, bundles):
        if not bundles:
            return None, None, []

        bitstream_records = []
        pdf_url = None
        original_filename = None

        for bundle in bundles:
            if not isinstance(bundle, dict):
                continue
            bundle_name = (bundle.get("name") or "").upper()
            bitstreams = (
                (bundle.get("_embedded") or {})
                .get("bitstreams", {})
                .get("_embedded", {})
                .get("bitstreams", [])
            )
            for bs in bitstreams:
                if not isinstance(bs, dict):
                    continue
                name = bs.get("name") or ""
                content_href = ((bs.get("_links") or {}).get("content") or {}).get("href")
                bitstream_records.append({
                    "bundle": bundle_name,
                    "name": name,
                    "sizeBytes": bs.get("sizeBytes"),
                    "url": content_href,
                })
                if bundle_name == "ORIGINAL" and content_href:
                    lower = name.lower()
                    if lower.endswith(".pdf") and pdf_url is None:
                        pdf_url = content_href
                        original_filename = name

        return pdf_url, original_filename, bitstream_records

    # ------------------------------------------------------------------
    # Generic text/meta helpers
    # ------------------------------------------------------------------

    def _make_soup(self, raw):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                last_exc = exc
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed: {exc}")
        print(f"[{self.site_id}] all BeautifulSoup parsers failed: {last_exc}")
        return None

    @staticmethod
    def _metadata_values(meta, key):
        out = []
        for item in meta.get(key) or []:
            if isinstance(item, dict) and item.get("value") is not None:
                out.append(str(item["value"]))
        return out

    def _all_meta(self, meta, *names):
        values = []
        for name in names:
            values.extend(self._metadata_values(meta, name))
        return self._dedupe([self._clean_text(v) for v in values])

    def _first_meta(self, meta, *names):
        for name in names:
            for value in self._metadata_values(meta, name):
                cleaned = self._clean_text(value)
                if cleaned:
                    return cleaned
        return ""

    @classmethod
    def _clean_text(cls, value):
        if value is None:
            return ""
        text = str(value)
        text = text.replace("\xa0", " ").replace("​", "")
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
        text = re.sub(r"[ \t\v]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        text = re.sub(r"\s+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def _one_line(cls, value):
        return re.sub(r"\s+", " ", cls._clean_text(value)).strip()

    @staticmethod
    def _normalize_date(value):
        value = (value or "").strip()
        match = re.match(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", value)
        if not match:
            return None
        if match.group(3):
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        if match.group(2):
            return f"{match.group(1)}-{match.group(2)}"
        return match.group(1)

    @staticmethod
    def _format_author_name(raw):
        """DSpace stores author names as "Family, Given". Reformat to
        "Given Family" so downstream comma-splitting (which treats commas
        as author separators) doesn't mangle a single name into two.
        """
        if not raw:
            return raw
        if "," in raw:
            family, _, given = raw.partition(",")
            family = family.strip()
            given = given.strip()
            if given and family:
                return f"{given} {family}"
        return raw

    @staticmethod
    def _dedupe(values):
        seen = set()
        out = []
        for value in values:
            if value is None:
                continue
            key = str(value).strip()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(key)
        return out
