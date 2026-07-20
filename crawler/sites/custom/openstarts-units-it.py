# -*- coding: utf-8 -*-
"""OpenstarTs crawler for the University of Trieste DSpace CRIS repository."""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class OpenstartsUnitsItCrawler(BaseCrawler):
    site_id = "openstarts-units-it"
    site_name = "Custom: openstarts-units-it"
    base_url = "https://www.openstarts.units.it"

    API_BASE = base_url + "/server/api"
    LIST_API = API_BASE + "/discover/search/objects"
    PAGE_SIZE = 25
    MAX_PAGES = 200
    MAX_SECONDS = 25 * 60
    STOP_MARGIN_SECONDS = 180
    BACKOFF_SECONDS = (1, 3, 9)
    MIN_SAVE_ABSTRACT_CHARS = 100

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url, *, context="request", accept="application/json,text/html,*/*;q=0.8"):
        """Fetch a URL with curl and return decoded text, or None after retries."""
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-4",
            "-skL",
            "--compressed",
            "--fail",
            "--connect-timeout",
            "15",
            "--max-time",
            "45",
            "-A",
            self.USER_AGENT,
            "-H",
            f"Accept: {accept}",
            "-H",
            "Accept-Language: en-US,en;q=0.9,it;q=0.8",
            url,
        ]

        last_error = "unknown error"
        for attempt, wait in enumerate(self.BACKOFF_SECONDS, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=55, check=False)
                if result.returncode == 0 and result.stdout:
                    return result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = stderr or f"curl exit {result.returncode}; empty response"
            except Exception as exc:
                last_error = str(exc)

            if attempt < len(self.BACKOFF_SECONDS):
                print(
                    f"[{self.site_id}] {context} failed attempt "
                    f"{attempt}/{len(self.BACKOFF_SECONDS)}: {last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] {context} failed after 3 attempts for {url}: {last_error}")
        return None

    def _curl_json(self, url, *, context="request"):
        raw = self._curl_get(url, context=context, accept="application/json,*/*;q=0.8")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] {context} returned invalid JSON: {exc}")
            return None

    # ------------------------------------------------------------------
    # Text and metadata helpers
    # ------------------------------------------------------------------

    def _parse_html(self, raw):
        """Parse malformed HTML defensively with parser fallback."""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception:
                continue
        return None

    def _clean_text(self, value):
        if value is None:
            return ""
        text = str(value)
        if "<" in text and ">" in text:
            soup = self._parse_html(text)
            if soup is not None:
                text = soup.get_text(" ")
        text = unescape(text)
        text = text.replace("\r", "\n")
        text = re.sub(r"[ \t\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        return text.strip()

    @staticmethod
    def _meta_values(meta, key):
        vals = meta.get(key) or []
        return [v.get("value") for v in vals if isinstance(v, dict) and v.get("value")]

    def _first_meta(self, meta, *keys):
        for key in keys:
            vals = self._meta_values(meta, key)
            if vals:
                return vals[0]
        return None

    def _meta_values_by_prefix(self, meta, *prefixes):
        values = []
        seen = set()
        for key in sorted(meta):
            if any(key == p or key.startswith(p + ".") for p in prefixes):
                for value in self._meta_values(meta, key):
                    cleaned = self._clean_text(value)
                    if cleaned and cleaned not in seen:
                        seen.add(cleaned)
                        values.append(cleaned)
        return values

    @staticmethod
    def _parse_date(raw):
        if not raw:
            return None
        text = str(raw).strip()
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        m = re.match(r"^(\d{4})-(\d{2})$", text)
        if m:
            return f"{m.group(1)}-{m.group(2)}-01"
        m = re.match(r"^(\d{4})$", text)
        if m:
            return f"{m.group(1)}-01-01"
        return None

    @staticmethod
    def _handle_to_post_number(handle):
        if not handle:
            return None
        match = re.search(r"/(\d+)(?:\D.*)?$", str(handle))
        return match.group(1) if match else str(handle)

    def _detail_page_url(self, item):
        handle = item.get("handle")
        if handle:
            return f"{self.base_url}/handle/{handle}"
        uuid = item.get("uuid") or item.get("id")
        return f"{self.base_url}/entities/publication/{uuid}" if uuid else self.base_url

    @staticmethod
    def _filename_from_url(url):
        if not url:
            return None
        path = urlparse(url).path.rstrip("/")
        tail = path.rsplit("/", 1)[-1]
        if "." in tail and len(tail) <= 200:
            return tail
        return None

    def _plain_metadata(self, meta):
        plain = {}
        for key, vals in (meta or {}).items():
            extracted = []
            for val in vals or []:
                if isinstance(val, dict) and val.get("value") is not None:
                    extracted.append(val.get("value"))
            if extracted:
                plain[key] = extracted
        return plain

    # ------------------------------------------------------------------
    # DSpace parsing
    # ------------------------------------------------------------------

    def _extract_objects(self, page_data):
        return (
            (page_data or {})
            .get("_embedded", {})
            .get("searchResult", {})
            .get("_embedded", {})
            .get("objects", [])
            or []
        )

    def _next_link(self, page_data):
        return (
            (page_data or {})
            .get("_embedded", {})
            .get("searchResult", {})
            .get("_links", {})
            .get("next", {})
            .get("href")
        )

    def _item_from_search_object(self, obj):
        return (obj or {}).get("_embedded", {}).get("indexableObject", {}) or {}

    def _fetch_item_detail(self, uuid):
        url = f"{self.API_BASE}/core/items/{uuid}?embed=bundles/bitstreams"
        return self._curl_json(url, context=f"item detail {uuid}")

    def _select_pdf(self, item):
        bundles = (
            (item or {})
            .get("_embedded", {})
            .get("bundles", {})
            .get("_embedded", {})
            .get("bundles", [])
            or []
        )
        candidates = []
        bitstream_summaries = []
        for bundle in bundles:
            bundle_name = bundle.get("name")
            bitstreams = (
                bundle.get("_embedded", {})
                .get("bitstreams", {})
                .get("_embedded", {})
                .get("bitstreams", [])
                or []
            )
            for bs in bitstreams:
                name = bs.get("name") or self._first_meta(bs.get("metadata") or {}, "dc.title")
                content_url = (bs.get("_links") or {}).get("content", {}).get("href")
                summary = {
                    "uuid": bs.get("uuid") or bs.get("id"),
                    "name": name,
                    "bundleName": bundle_name,
                    "sizeBytes": bs.get("sizeBytes"),
                    "sequenceId": bs.get("sequenceId"),
                    "contentUrl": content_url,
                }
                bitstream_summaries.append({k: v for k, v in summary.items() if v not in (None, "")})
                if bundle_name == "ORIGINAL" and name and name.lower().endswith(".pdf"):
                    candidates.append(bs)

        if not candidates:
            return None, None, bitstream_summaries

        candidates.sort(key=lambda bs: (bs.get("sizeBytes") or 0), reverse=True)
        chosen = candidates[0]
        pdf_url = (chosen.get("_links") or {}).get("content", {}).get("href")
        original_filename = (
            chosen.get("name")
            or self._first_meta(chosen.get("metadata") or {}, "dc.title")
            or self._filename_from_url(pdf_url)
        )
        return pdf_url, original_filename, bitstream_summaries

    def _extract_doi(self, meta):
        doi = self._first_meta(meta, "dc.identifier.doi", "dc.identifier.doi[]")
        if doi:
            return doi
        for value in self._meta_values(meta, "dc.identifier.uri"):
            if "doi.org/" in value:
                return value.rsplit("doi.org/", 1)[-1].strip()
        for key in meta:
            if "doi" in key.lower():
                vals = self._meta_values(meta, key)
                if vals:
                    return vals[0]
        return None

    def _paper_from_item(self, item, list_item=None):
        meta = item.get("metadata") or {}
        list_meta = (list_item or {}).get("metadata") or {}
        uuid = item.get("uuid") or item.get("id") or (list_item or {}).get("uuid") or (list_item or {}).get("id")
        handle = item.get("handle") or (list_item or {}).get("handle")

        title = self._clean_text(item.get("name") or self._first_meta(meta, "dc.title"))
        abstract_values = self._meta_values(meta, "dc.description.abstract")
        if not abstract_values and list_meta:
            abstract_values = self._meta_values(list_meta, "dc.description.abstract")
        abstract = "\n\n".join(self._clean_text(v) for v in abstract_values if self._clean_text(v))
        if not abstract:
            fallback_values = []
            for key in ("dc.description.tableofcontents", "dc.description"):
                fallback_values.extend(self._meta_values(meta, key))
            abstract = "\n\n".join(self._clean_text(v) for v in fallback_values if self._clean_text(v))

        issued_raw = self._first_meta(meta, "dc.date.issued", "dc.date.created", "dc.date.available")
        listed_raw = self._first_meta(meta, "dc.date.accessioned", "dc.date.available")
        published_date = self._parse_date(issued_raw)
        listed_date = self._parse_date(listed_raw)

        authors_list = self._meta_values(meta, "dc.contributor.author")
        editor_list = self._meta_values(meta, "dc.contributor.editor")
        contributor_list = self._meta_values(meta, "dc.contributor")
        authors_source = authors_list or editor_list or contributor_list
        authors = "; ".join(self._clean_text(a) for a in authors_source if self._clean_text(a)) or None

        publisher_values = self._meta_values(meta, "dc.publisher")
        publisher = "; ".join(self._clean_text(p) for p in publisher_values if self._clean_text(p)) or None

        department_values = []
        for key in (
            "oairecerif.author.affiliation",
            "oairecerif.editor.affiliation",
            "dc.description.archiveowner",
        ):
            department_values.extend(self._meta_values(meta, key))
        department_values = [self._clean_text(v) for v in department_values if self._clean_text(v)]
        department = "; ".join(dict.fromkeys(department_values)) or None
        if not publisher and department:
            publisher = department

        journal = self._clean_text(
            self._first_meta(
                meta,
                "dc.relation.ispartof",
                "dc.source",
                "dc.identifier.citation",
            )
            or ""
        ) or None
        keywords_list = self._meta_values_by_prefix(meta, "dc.subject")
        keywords = ", ".join(keywords_list) or None
        category = self._clean_text(self._first_meta(meta, "dc.type", "dspace.entity.type") or "") or None
        doi = self._extract_doi(meta)

        pdf_url, original_filename, bitstreams = self._select_pdf(item)
        detail_url = self._detail_page_url(item)
        post_number = self._handle_to_post_number(handle)

        metadata = {
            "posted_date": listed_raw,
            "originalFilename": original_filename,
            "journal_raw": journal,
            "series": self._first_meta(meta, "dc.relation.ispartofseries", "dc.relation.ispartofseries[]"),
            "volume": self._first_meta(meta, "oaire.citation.volume", "dc.relation.volume"),
            "issue": self._first_meta(meta, "oaire.citation.issue", "dc.relation.issue"),
            "node_id": uuid,
            "uuid": uuid,
            "handle": handle,
            "post_number": post_number,
            "entity_type": self._first_meta(meta, "dspace.entity.type"),
            "bitstreams": bitstreams,
            "raw_metadata": self._plain_metadata(meta),
            "list_raw_metadata": self._plain_metadata(list_meta) if list_meta else None,
            "editors": editor_list or None,
            "contributors": contributor_list or None,
            "isbn": self._first_meta(meta, "dc.identifier.isbn", "dc.identifier.eisbn"),
            "issn": self._first_meta(meta, "dc.identifier.issn", "dc.identifier.eissn"),
            "citation": self._first_meta(meta, "dc.identifier.citation"),
            "language": self._first_meta(meta, "dc.language.iso"),
            "rights": self._first_meta(meta, "dc.rights"),
            "rights_uri": self._first_meta(meta, "dc.rights.uri"),
        }
        metadata = {k: v for k, v in metadata.items() if v not in (None, "", [], {})}

        return {
            "id": uuid,
            "site_id": self.site_id,
            "external_id": uuid,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": authors,
            "publisher": publisher,
            "department": department,
            "journal": journal,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Crawl
    # ------------------------------------------------------------------

    def _near_time_budget(self, start_time):
        return time.time() - start_time > self.MAX_SECONDS - self.STOP_MARGIN_SECONDS

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        start_time = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

        for page in range(self.MAX_PAGES):
            if limit is not None and saved >= limit:
                break
            if self._near_time_budget(start_time):
                print(f"[{self.site_id}] wall-clock budget nearly exhausted; exiting cleanly")
                break

            if (page + 1) % 10 == 0:
                print(f"[{self.site_id}] page {page + 1}: saved {saved}/{limit_or_inf}")

            list_url = (
                f"{self.LIST_API}?dsoType=item&size={self.PAGE_SIZE}&page={page}"
                "&sort=dc.date.accessioned,DESC"
            )
            page_data = self._curl_json(list_url, context=f"list page {page + 1}")
            if page_data is None:
                print(f"[{self.site_id}] failed to fetch list page {page + 1}; stopping")
                break

            objects = self._extract_objects(page_data)
            if not objects:
                print(f"[{self.site_id}] page {page + 1}: no records; stopping")
                break

            new_urls_on_page = 0
            for obj in objects:
                if limit is not None and saved >= limit:
                    break
                if self._near_time_budget(start_time):
                    print(f"[{self.site_id}] wall-clock budget nearly exhausted; exiting cleanly")
                    return saved

                list_item = self._item_from_search_object(obj)
                uuid = list_item.get("uuid") or list_item.get("id")
                item_label = uuid or f"page-{page + 1}-item"

                try:
                    if not uuid:
                        print(f"[{self.site_id}] item {item_label} skipped: missing uuid")
                        continue

                    detail_url = self._detail_page_url(list_item)
                    if detail_url in seen_urls:
                        print(f"[{self.site_id}] item {item_label} skipped: duplicate URL {detail_url}")
                        continue
                    seen_urls.add(detail_url)
                    new_urls_on_page += 1

                    time.sleep(self._delay)
                    item = self._fetch_item_detail(uuid)
                    if not item:
                        raise RuntimeError("detail API returned no data")

                    paper = self._paper_from_item(item, list_item=list_item)
                    abstract_len = len(paper.get("abstract") or "")
                    if abstract_len < 50:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({abstract_len} chars)"
                        )
                        continue
                    if abstract_len < self.MIN_SAVE_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract below save threshold ({abstract_len} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:90]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if new_urls_on_page == 0:
                print(f"[{self.site_id}] page {page + 1}: 0 new records; stopping")
                break
            if not self._next_link(page_data):
                print(f"[{self.site_id}] page {page + 1}: next page absent; stopping")
                break
        else:
            print(f"[{self.site_id}] reached safety cap of {self.MAX_PAGES} pages")

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved
