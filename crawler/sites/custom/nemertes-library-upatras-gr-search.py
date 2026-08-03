# -*- coding: utf-8 -*-
"""Crawler for Nemertes (University of Patras institutional repository).

Target: https://nemertes.library.upatras.gr/search?...&f.has_content_in_original_bundle=true,equals

Nemertes is a DSpace 7.6.1 repository. The Angular frontend is a pure
client-side SPA (no server-side rendering), so item pages return only an
empty shell to non-browser clients. All data is instead pulled straight
from the DSpace REST API (``/server/api/...``), which returns full item
metadata (including abstracts) inline in the search-result listing — no
separate per-item detail fetch is needed. One extra request per item is
still required to resolve the bitstream (PDF) download URL.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from urllib.parse import urlencode

from crawler.base_crawler import BaseCrawler


class NemertesLibraryUpatrasGrSearchCrawler(BaseCrawler):
    site_id = "nemertes-library-upatras-gr-search"
    site_name = "Custom: nemertes-library-upatras-gr-search"
    base_url = "https://nemertes.library.upatras.gr"

    START_URL = (
        "https://nemertes.library.upatras.gr/search?bbm.page=6&spc.page=1"
        "&spc.sf=dc.date.accessioned&spc.sd=DESC"
        "&f.has_content_in_original_bundle=true,equals"
    )
    LIST_API_URL = f"{base_url}/server/api/discover/search/objects"
    ITEM_BUNDLES_API = f"{base_url}/server/api/core/items/{{uuid}}/bundles"

    PAGE_SIZE = 100
    MAX_PAGES = 200
    MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    MIN_ABSTRACT_CHARS = 50
    BACKOFF_SECONDS = (1, 3, 9)

    def crawl(self, limit=None):
        limit_label = str(limit) if limit is not None else "inf"
        print(f"[{self.site_id}] starting crawl limit={limit_label}")
        start_time = time.time()

        saved = 0
        seen_urls = set()
        page = 0
        item_number = 0

        while True:
            if limit is not None and saved >= limit:
                break

            if page >= self.MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {self.MAX_PAGES} pages reached; stopping")
                break

            elapsed = time.time() - start_time
            if elapsed > self.MAX_SECONDS:
                print(f"[{self.site_id}] 25-minute budget reached; exiting cleanly at saved={saved}")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            list_data = self._fetch_list(page)
            if not list_data:
                print(f"[{self.site_id}] list API failed at page {page}; stopping")
                break

            records = self._list_records(list_data)
            if not records:
                print(f"[{self.site_id}] no records returned at page {page}; stopping")
                break

            new_on_page = 0
            for record in records:
                if limit is not None and saved >= limit:
                    break

                item_number += 1
                try:
                    item = self._indexable_object(record)
                    uuid = (item.get("uuid") or item.get("id") or "").strip()
                    if not uuid:
                        raise RuntimeError("list record has no item uuid")

                    handle = (item.get("handle") or "").strip()
                    url = f"{self.base_url}/handle/{handle}" if handle else f"{self.base_url}/items/{uuid}"
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    new_on_page += 1

                    time.sleep(self._delay)
                    bitstreams = self._fetch_original_bitstreams(uuid)
                    parsed = self._parse_item(item, uuid, handle, url, bitstreams)

                    abstract = parsed["abstract"]
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_number} ({uuid}) skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(parsed["paper"])
                    saved += 1
                    counter = f"{saved}/{limit}" if limit else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {parsed['paper']['title'][:90]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_number} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: all records already seen; stopping")
                break

            page_meta = self._page_meta(list_data)
            total_pages = page_meta.get("totalPages")
            if total_pages is not None and page + 1 >= int(total_pages):
                print(f"[{self.site_id}] reached last page ({total_pages}); stopping")
                break
            if len(records) < self.PAGE_SIZE:
                break

            page += 1

        print(f"[{self.site_id}] done. total saved={saved}")
        return saved

    # ------------------------------------------------------------------
    # Fetch helpers
    # ------------------------------------------------------------------

    def _fetch_list(self, page):
        params = {
            "dsoType": "item",
            "sort": "dc.date.accessioned,DESC",
            "page": str(page),
            "size": str(self.PAGE_SIZE),
            "query": "",
            "f.has_content_in_original_bundle": "true,equals",
        }
        url = f"{self.LIST_API_URL}?{urlencode(params)}"
        return self._curl_json(url, context=f"list page={page}")

    def _fetch_original_bitstreams(self, uuid):
        url = f"{self.ITEM_BUNDLES_API.format(uuid=uuid)}?embed=bitstreams"
        data = self._curl_json(url, context=f"item {uuid} bundles")
        bundles = data.get("_embedded", {}).get("bundles", []) if data else []
        if not isinstance(bundles, list):
            return []

        ordered = sorted(bundles, key=lambda b: 0 if b.get("name") == "ORIGINAL" else 1)
        bitstreams = []
        for bundle in ordered:
            embedded = bundle.get("_embedded", {}).get("bitstreams", {})
            found = embedded.get("_embedded", {}).get("bitstreams", []) if isinstance(embedded, dict) else []
            if not found:
                href = bundle.get("_links", {}).get("bitstreams", {}).get("href")
                if href:
                    fallback = self._curl_json(href, context=f"item {uuid} bundle {bundle.get('name')} bitstreams")
                    found = fallback.get("_embedded", {}).get("bitstreams", []) if fallback else []
            if isinstance(found, list):
                for b in found:
                    b["_bundleName"] = bundle.get("name")
                    bitstreams.append(b)
            if bundle.get("name") == "ORIGINAL" and bitstreams:
                break
        return bitstreams

    def _curl_json(self, url, context="request"):
        raw = self._curl_get(url, context=context)
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

    def _curl_get(self, url, context="request"):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--max-time",
            "45",
            "--connect-timeout",
            "15",
            "-A",
            self.USER_AGENT,
            "-H",
            "Accept: application/json,text/javascript,*/*;q=0.8",
            "-H",
            "Accept-Language: en-US,en;q=0.9,el;q=0.8",
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
            print(f"[{self.site_id}] {context} curl failed (attempt {attempt + 1}/3): {last_error}")
            if attempt < 2:
                time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_item(self, item, uuid, handle, url, bitstreams):
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}

        title = self._metadata_first(metadata, ("dc.title",)) or (item.get("name") or "").strip()
        if not title:
            raise RuntimeError(f"{uuid} has no title")

        abstract = self._metadata_first(metadata, ("dc.description.abstract",))
        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            translated = self._metadata_first(metadata, ("dc.description.translatedabstract",))
            if len(translated) > len(abstract):
                abstract = translated

        authors = [
            self._format_author_name(a)
            for a in self._metadata_values(metadata, ("dc.contributor.author",))
        ]
        publisher = self._metadata_values(metadata, ("dc.publisher",))
        keywords = self._dedupe(
            self._metadata_values(metadata, ("dc.subject",))
            + self._metadata_values(metadata, ("dc.subject.alternative",))
        )
        category = self._metadata_first(metadata, ("dc.type",)) or self._metadata_first(metadata, ("dc.degree",))
        doi = self._normalize_doi(self._metadata_first(metadata, ("dc.identifier.doi",)))
        journal = self._metadata_first(metadata, ("dc.relation.ispartofjournal", "dc.source"))

        raw_issued = self._metadata_first(metadata, ("dc.date.issued",))
        published_date = self._normalize_date(raw_issued)

        raw_accessioned = self._metadata_first(metadata, ("dc.date.accessioned",))
        posted_date = raw_accessioned[:10] if len(raw_accessioned) >= 10 else None

        pdf_url, original_filename = self._select_pdf(bitstreams)

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
            "series": None,
            "volume": None,
            "issue": None,
            "item_uuid": uuid,
            "handle": handle or None,
            "degree": self._metadata_first(metadata, ("dc.degree",)) or None,
            "dc_type": self._metadata_first(metadata, ("dc.type",)) or None,
            "language": self._metadata_first(metadata, ("dc.language.iso",)) or None,
            "rights": self._metadata_first(metadata, ("dc.rights",)) or None,
            "rights_uri": self._metadata_first(metadata, ("dc.rights.uri",)) or None,
            "title_alternative": self._metadata_first(metadata, ("dc.title.alternative",)) or None,
            "advisor": self._metadata_values(metadata, ("dc.contributor.advisor",)) or None,
            "supervisor": self._metadata_values(metadata, ("datacite.contributor.Supervisor",)) or None,
            "related_person": self._metadata_values(metadata, ("datacite.contributor.RelatedPerson",)) or None,
            "contributor_other": self._metadata_values(metadata, ("dc.contributor.other",)) or None,
            "sponsorship": self._metadata_first(metadata, ("dc.description.sponsorship",)) or None,
            "translated_abstract": self._metadata_first(metadata, ("dc.description.translatedabstract",)) or None,
        }
        metadata_raw = {k: v for k, v in metadata_raw.items() if v not in (None, "", [])}

        paper = {
            "id": None,
            "site_id": self.site_id,
            "external_id": uuid,
            "post_number": post_number,
            "title": self._clean_text(title),
            "abstract": self._clean_text(abstract),
            "published_date": published_date,
            "posted_date": posted_date,
            "authors": "; ".join(authors) if authors else None,
            "publisher": "; ".join(publisher) if publisher else None,
            "department": None,
            "journal": journal or None,
            "url": url,
            "pdf_url": pdf_url,
            "keywords": ", ".join(keywords) if keywords else None,
            "category": category or None,
            "doi": doi or None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata_raw, ensure_ascii=False),
        }
        return {"paper": paper, "abstract": paper["abstract"]}

    def _select_pdf(self, bitstreams):
        for b in bitstreams:
            name = (b.get("name") or "").strip()
            href = b.get("_links", {}).get("content", {}).get("href") or ""
            if name.lower().endswith(".pdf") and href:
                return href, name
        for b in bitstreams:
            if b.get("_bundleName") == "ORIGINAL":
                href = b.get("_links", {}).get("content", {}).get("href") or ""
                name = (b.get("name") or "").strip()
                if href:
                    return href, (name or None)
        return None, None

    # ------------------------------------------------------------------
    # Small utilities
    # ------------------------------------------------------------------

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

    def _metadata_first(self, metadata, keys):
        values = self._metadata_values(metadata, keys)
        return values[0] if values else ""

    def _metadata_values(self, metadata, keys):
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
            return None
        text = str(value)
        match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
        if match:
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        match = re.search(r"(\d{4})-(\d{2})", text)
        if match:
            return f"{match.group(1)}-{match.group(2)}-01"
        match = re.search(r"\b(\d{4})\b", text)
        if match:
            return f"{match.group(1)}-01-01"
        return None

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
        text = re.sub(r"\r\n|\r|\n|\t", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()
