# -*- coding: utf-8 -*-
"""Crawler for FDFA/EDA English news.

The public EDA landing page is an AEM page, but the records are served by
the Swiss federal Content Broker API:

    https://d-nsbc-p.admin.ch/v1/search
    https://d-nsbc-p.admin.ch/v1/languageGroupId/{langGroupId}

The public detail URL for each result is the supplied EDA news listing path
plus /content/eda/en/meta/news/YYYY/M/D/{langGroupId}.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timezone
from html import unescape
from urllib.parse import unquote, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class EdaAdminChEdaCrawler(BaseCrawler):
    site_id = "eda-admin-ch-eda"
    site_name = "Custom: eda-admin-ch-eda"
    base_url = "https://www.eda.admin.ch"

    START_URL = "https://www.eda.admin.ch/eda/en/fdfa/fdfa/aktuell/news.html"
    API_BASE = "https://d-nsbc-p.admin.ch/v1"
    SEARCH_URL = f"{API_BASE}/search"
    DETAIL_URL_TEMPLATE = f"{API_BASE}/languageGroupId/{{lang_group_id}}"

    PUBLISHER_ID = "2"  # Federal Department of Foreign Affairs (EDA/FDFA)
    PAGE_SIZE = 50
    SAFETY_PAGE_CAP = 200
    WALL_CLOCK_SECONDS = 25 * 60
    WALL_APPROACH_SECONDS = 24 * 60
    CURL_TIMEOUT = 60
    BACKOFF_SECONDS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 50
    MAX_ABSTRACT_CHARS = 6000
    CURL_MARKER = "__EDA_ADMIN_CH_EDA_CURL_META__:"

    CATEGORY_LABELS = {
        "medienmitteilung": "Press release",
        "rede": "Speech",
        "news": "News",
        "fremdmitteilung": "External announcement",
        "newsletter": "Newsletter",
    }

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self._detail_delay = delay if detail_delay is None else detail_delay

    def crawl(self, limit=None):
        """Crawl FDFA/EDA English news and persist documents."""
        if limit is not None and limit <= 0:
            return 0

        saved = 0
        page = 0
        offset = 0
        seen_urls: set[str] = set()
        started_at = time.monotonic()
        limit_or_inf = str(limit) if limit is not None else "inf"

        org_map = self._load_organisation_map()
        topic_map = self._load_topic_map()

        while True:
            if limit is not None and saved >= limit:
                break

            elapsed = time.monotonic() - started_at
            if elapsed >= self.WALL_APPROACH_SECONDS:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget, stopping cleanly")
                break

            if page >= self.SAFETY_PAGE_CAP:
                print(f"[{self.site_id}] safety cap of {self.SAFETY_PAGE_CAP} pages reached")
                break

            page += 1
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            search_data = self._fetch_search_page(offset)
            if not search_data:
                print(f"[{self.site_id}] page {page}: search endpoint returned no data, stopping")
                break

            items = search_data.get("items") or []
            if not items:
                print(f"[{self.site_id}] page {page}: 0 records, stopping")
                break

            new_urls_on_page = 0

            for idx, item in enumerate(items, start=1):
                if limit is not None and saved >= limit:
                    break

                elapsed = time.monotonic() - started_at
                if elapsed >= self.WALL_APPROACH_SECONDS:
                    print(f"[{self.site_id}] approaching 25-minute wall-clock budget, stopping cleanly")
                    return saved

                item_label = f"page {page} item {idx}"
                try:
                    list_record = self._parse_search_item(item)
                    if not list_record:
                        continue

                    detail_url = list_record.get("url") or ""
                    if not detail_url:
                        print(f"[{self.site_id}] item {item_label} skipped: no detail URL")
                        continue
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_urls_on_page += 1

                    if self._detail_delay:
                        time.sleep(self._detail_delay)

                    detail_record = self._fetch_detail_record(list_record["external_id"])
                    if detail_record is None:
                        print(f"[{self.site_id}] item {item_label} skipped: detail fetch failed")
                        continue

                    paper = self._build_paper(
                        list_record=list_record,
                        search_item=item,
                        detail_record=detail_record,
                        org_map=org_map,
                        topic_map=topic_map,
                    )
                    if not paper:
                        continue

                    abstract_len = len(paper.get("abstract") or "")
                    if abstract_len < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({abstract_len} chars)"
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

            if limit is not None and saved >= limit:
                break

            if new_urls_on_page == 0:
                print(f"[{self.site_id}] page {page}: 0 new records, stopping")
                break

            total = search_data.get("pageResults")
            if isinstance(total, int) and offset + self.PAGE_SIZE >= total:
                print(f"[{self.site_id}] reached end of pagination at {total} results")
                break

            offset += self.PAGE_SIZE

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # API fetches
    # ------------------------------------------------------------------

    def _fetch_search_page(self, offset):
        end_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT23:59:59.999Z")
        params = [
            ("languages", "en"),
            ("newsKinds", "CONTENT_HUB"),
            ("newsKinds", "ONSB"),
            ("publisherIDs", self.PUBLISHER_ID),
            ("start_date", "2000-01-01T00:00:00.000Z"),
            ("end_date", end_date),
            ("offset", str(offset)),
            ("limit", str(self.PAGE_SIZE)),
            ("sort", "DESC"),
        ]
        raw = self._curl_text(
            self.SEARCH_URL + "?" + urlencode(params),
            context=f"search offset={offset}",
            accept="application/json",
            referer=self.START_URL,
        )
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError as exc:
            print(f"[{self.site_id}] search JSON parse failed at offset={offset}: {exc}")
            return None
        return data if isinstance(data, dict) else None

    def _fetch_detail_record(self, lang_group_id):
        if not lang_group_id:
            return None
        raw = self._curl_text(
            self.DETAIL_URL_TEMPLATE.format(lang_group_id=lang_group_id),
            context=f"detail {lang_group_id}",
            accept="application/json",
            referer=self.START_URL,
        )
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError as exc:
            print(f"[{self.site_id}] detail JSON parse failed for {lang_group_id}: {exc}")
            return None
        if isinstance(data, list):
            return self._pick_language_record(data, "en")
        return data if isinstance(data, dict) else None

    def _load_organisation_map(self):
        raw = self._curl_text(
            f"{self.API_BASE}/organisations?language=en&nsbEnabled=true",
            context="organisations",
            accept="application/json",
            referer=self.START_URL,
        )
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}

        orgs = {}

        def walk(nodes):
            for node in nodes or []:
                if not isinstance(node, dict):
                    continue
                raw_id = node.get("id")
                label = self._clean_text(node.get("label") or node.get("name") or "")
                acronym = self._clean_text(node.get("acronym") or "")
                if raw_id is not None and label:
                    if acronym and acronym.lower() not in label.lower():
                        label = f"{label} ({acronym})"
                    orgs[str(raw_id)] = label
                walk(node.get("children") or [])

        walk(data if isinstance(data, list) else [])
        return orgs

    def _load_topic_map(self):
        raw = self._curl_text(
            f"{self.API_BASE}/topicsSubscriber?language=en",
            context="topics",
            accept="application/json",
            referer=self.START_URL,
        )
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}

        topics = {}

        def walk(nodes):
            for node in nodes or []:
                if not isinstance(node, dict):
                    continue
                raw_id = node.get("id")
                label = self._clean_text(node.get("label") or "")
                if raw_id is not None and label:
                    topics[str(raw_id)] = label
                walk(node.get("children") or [])

        walk(data if isinstance(data, list) else [])
        return topics

    # ------------------------------------------------------------------
    # Record building
    # ------------------------------------------------------------------

    def _parse_search_item(self, item):
        if not isinstance(item, dict):
            return None

        if item.get("lang") and item.get("lang") != "en":
            return None

        lang_group_id = self._clean_text(item.get("langGroupId") or "")
        api_id = self._clean_text(item.get("id") or "")
        external_id = lang_group_id or api_id
        if not external_id:
            return None

        content = item.get("content") or {}
        metadata = content.get("metadata") or {}
        systemdata = content.get("systemdata") or {}

        title = self._clean_text(
            item.get("title")
            or metadata.get("title")
            or metadata.get("metaTitle")
            or systemdata.get("title")
        )
        if not title:
            return None

        publish_raw = item.get("publishDate") or systemdata.get("visiblePublicationDate") or ""
        listed_date = self._iso_date(publish_raw)
        document_id = systemdata.get("documentId")
        publication_id = systemdata.get("publicationId")
        post_number = self._post_number(document_id, publication_id, api_id)
        detail_url = self._public_detail_url(lang_group_id, metadata, systemdata, publish_raw)

        return {
            "api_id": api_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": self._clean_html(item.get("description") or ""),
            "listed_date": listed_date,
            "listed_date_raw": publish_raw,
            "url": detail_url,
            "category_raw": item.get("newsCategory") or metadata.get("gdNewsCategory"),
            "topics": item.get("topics") or metadata.get("gdSubscriberTopicLevel1") or [],
            "publishers": item.get("publishers") or [],
            "co_publishers": item.get("coPublishers") or metadata.get("nsbNewsCoPublisher") or [],
            "location": item.get("location") or metadata.get("newsLocation"),
            "systemdata": systemdata,
            "metadata": metadata,
        }

    def _build_paper(self, list_record, search_item, detail_record, org_map, topic_map):
        detail = detail_record if isinstance(detail_record, dict) else {}
        content = detail.get("content") or {}
        document = content.get("document") or {}
        metadata = document.get("metadata") or detail.get("content", {}).get("metadata") or {}
        if not metadata:
            metadata = list_record.get("metadata") or {}
        systemdata = document.get("systemdata") or list_record.get("systemdata") or {}

        title = self._clean_text(
            metadata.get("title")
            or metadata.get("metaTitle")
            or detail.get("title")
            or list_record.get("title")
        )
        if not title:
            return None

        published_raw = (
            metadata.get("announcementDate")
            or systemdata.get("visiblePublicationDate")
            or systemdata.get("firstPublicationDate")
            or detail.get("publishDate")
            or list_record.get("listed_date_raw")
        )
        published_date = self._iso_date(published_raw)
        listed_raw = list_record.get("listed_date_raw") or detail.get("publishDate") or published_raw
        listed_date = list_record.get("listed_date") or self._iso_date(listed_raw)

        abstract = self._extract_abstract(
            metadata=metadata,
            document=document,
            detail=detail,
            fallback=list_record.get("abstract"),
        )[: self.MAX_ABSTRACT_CHARS]

        topic_ids = self._as_string_list(detail.get("topics") or list_record.get("topics"))
        keywords = self._join_unique([topic_map.get(t, t) for t in topic_ids if t], sep=",")

        publisher_ids = self._as_string_list(detail.get("publishers") or list_record.get("publishers"))
        co_publisher_ids = self._as_string_list(
            detail.get("coPublishers") or list_record.get("co_publishers")
        )
        publisher = self._join_unique(
            [org_map.get(pid, pid) for pid in publisher_ids + co_publisher_ids if pid],
            sep=";",
        )
        department = org_map.get(
            self.PUBLISHER_ID,
            "Federal Department of Foreign Affairs (FDFA)",
        )

        category_raw = detail.get("newsCategory") or list_record.get("category_raw")
        category = self.CATEGORY_LABELS.get(str(category_raw or ""), category_raw or "News")
        pdf_url = self._find_pdf_url(detail) or self._find_pdf_url(search_item)
        original_filename = self._filename_from_url(pdf_url)
        if pdf_url and not original_filename:
            original_filename = self._content_disposition_filename(pdf_url)

        document_id = systemdata.get("documentId") or list_record.get("systemdata", {}).get("documentId")
        publication_id = systemdata.get("publicationId") or list_record.get("systemdata", {}).get("publicationId")
        post_number = list_record.get("post_number") or self._post_number(
            document_id,
            publication_id,
            detail.get("id") or list_record.get("api_id"),
        )

        raw_metadata = {
            "posted_date": listed_raw,
            "listed_date": listed_date,
            "published_date_raw": published_raw,
            "originalFilename": original_filename,
            "journal_raw": metadata.get("journal") or metadata.get("journal_raw"),
            "series": metadata.get("series"),
            "volume": metadata.get("volume"),
            "issue": metadata.get("issue"),
            "api_id": detail.get("id") or list_record.get("api_id"),
            "node_id": document_id,
            "documentId": document_id,
            "publicationId": publication_id,
            "langGroupId": list_record.get("external_id"),
            "post_number": post_number,
            "newsCategory": category_raw,
            "publishers": publisher_ids,
            "coPublishers": co_publisher_ids,
            "topics": topic_ids,
            "location": detail.get("location") or list_record.get("location") or metadata.get("newsLocation"),
            "slug": metadata.get("slug"),
            "source_start_url": self.START_URL,
            "source_api": self.SEARCH_URL,
            "detail_api": self.DETAIL_URL_TEMPLATE.format(
                lang_group_id=list_record.get("external_id")
            ),
            "detail_metadata": metadata,
            "detail_systemdata": systemdata,
            "detail_references": content.get("references") or {},
            "raw_search_item": search_item,
            "raw_detail_record": detail,
        }

        return {
            "id": f"{self.site_id}:{list_record.get('external_id')}",
            "site_id": self.site_id,
            "external_id": list_record.get("external_id"),
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": None,
            "publisher": publisher or department,
            "department": department,
            "journal": None,
            "url": list_record["url"],
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": metadata.get("doi"),
            "original_filename": original_filename,
            "metadata": json.dumps(raw_metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------

    def _extract_abstract(self, metadata, document, detail, fallback=""):
        parts = []
        for key in ("description", "metaDescription", "openGraphDescription"):
            text = self._clean_html(metadata.get(key) or "")
            if text:
                parts.append(text)
                break

        parts.extend(self._extract_component_texts(document.get("content") or []))

        for html_text in detail.get("text") or []:
            text = self._clean_html(html_text)
            if text:
                parts.append(text)

        if fallback:
            parts.append(self._clean_html(fallback))

        return self._join_unique(parts, sep=" ")

    def _extract_component_texts(self, value):
        parts = []

        def walk(obj):
            if isinstance(obj, dict):
                content = obj.get("content")
                if isinstance(content, dict):
                    for key in ("title", "lead", "text", "description", "caption"):
                        text = self._clean_html(content.get(key) or "")
                        if text:
                            parts.append(text)
                for key in ("containers", "nestedContent", "items", "children"):
                    nested = obj.get(key)
                    if isinstance(nested, dict):
                        for child in nested.values():
                            walk(child)
                    elif isinstance(nested, list):
                        walk(nested)
                for key, child in obj.items():
                    if key in {"content", "containers", "nestedContent", "items", "children"}:
                        continue
                    if isinstance(child, (dict, list)):
                        walk(child)
            elif isinstance(obj, list):
                for child in obj:
                    walk(child)

        walk(value)
        return parts

    def _find_pdf_url(self, value):
        candidates = []

        def add_candidate(raw):
            if not raw:
                return
            text = unescape(str(raw).strip())
            if not text:
                return
            match = re.search(r"https?://[^\s\"'<>]+?\.pdf(?:[^\s\"'<>]*)?", text, re.I)
            if match:
                candidates.append(match.group(0))
                return
            if ".pdf" in text.lower():
                candidates.append(text)

        def walk(obj):
            if isinstance(obj, dict):
                asset = obj.get("asset")
                if isinstance(asset, dict):
                    mime = str(asset.get("mimeType") or "").lower()
                    if "pdf" in mime:
                        add_candidate(asset.get("url") or asset.get("uri") or asset.get("href"))
                for key, child in obj.items():
                    if key in ("url", "uri", "href", "originalUrl", "downloadUrl", "src"):
                        add_candidate(child)
                    elif key == "text" and isinstance(child, str):
                        for href in self._hrefs_from_html(child):
                            add_candidate(href)
                    walk(child)
            elif isinstance(obj, list):
                for child in obj:
                    walk(child)
            elif isinstance(obj, str) and ".pdf" in obj.lower():
                for href in self._hrefs_from_html(obj):
                    add_candidate(href)
                add_candidate(obj)

        walk(value)
        for candidate in candidates:
            normalized = self._normalize_pdf_url(candidate)
            if normalized:
                return normalized
        return None

    def _hrefs_from_html(self, raw):
        soup = self._make_soup(raw, context="html fragment")
        if soup is None:
            return []
        return [a.get("href") for a in soup.find_all("a", href=True)]

    def _normalize_pdf_url(self, url):
        url = self._clean_text(url)
        if not url or ".pdf" not in url.lower():
            return None
        if url.startswith("//"):
            url = "https:" + url
        url = urljoin(self.base_url, url)
        match = re.search(r"(.+?\.pdf)(?:[)\]\s].*)?$", url, re.I)
        if match:
            url = match.group(1)
        return url

    def _public_detail_url(self, lang_group_id, metadata, systemdata, publish_raw):
        if not lang_group_id:
            return self.START_URL
        date_raw = metadata.get("announcementDate") or publish_raw
        date_part = self._date_path(date_raw)
        if not date_part:
            date_part = self._date_path(systemdata.get("visiblePublicationDate") or "")
        if date_part:
            return f"{self.START_URL}/content/eda/en/meta/news/{date_part}/{lang_group_id}"
        return f"{self.START_URL}/content/eda/en/meta/news/{lang_group_id}"

    def _date_path(self, value):
        date = self._iso_date(value)
        if not date:
            return None
        year, month, day = date.split("-")
        return f"{year}/{int(month)}/{int(day)}"

    # ------------------------------------------------------------------
    # Network and parsing utilities
    # ------------------------------------------------------------------

    def _curl_text(self, url, context="request", accept=None, referer=None):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--connect-timeout",
            "15",
            "--max-time",
            str(self.CURL_TIMEOUT),
            "-A",
            self.USER_AGENT,
            "-H",
            f"Accept: {accept or 'application/json,text/html,application/xhtml+xml,*/*;q=0.8'}",
            "-H",
            "Accept-Language: en-GB,en;q=0.9",
            "-w",
            "\n" + self.CURL_MARKER + "%{http_code}\t%{url_effective}",
            url,
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
            cmd.extend(["-H", f"Origin: {self.base_url}"])

        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=self.CURL_TIMEOUT + 10)
                body, code, _ = self._split_curl_output(result.stdout or b"", url)
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                if result.returncode != 0:
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if code and code.isdigit() and int(code) >= 400:
                    raise RuntimeError(f"HTTP {code}")
                if not body:
                    raise RuntimeError("empty response")
                return body.decode("utf-8", errors="replace")
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                last_error = str(exc)
                print(f"[{self.site_id}] {context} curl failed (attempt {attempt}/3): {last_error}")
                if attempt < 3:
                    wait = self.BACKOFF_SECONDS[attempt - 1]
                    print(f"[{self.site_id}] retrying in {wait}s...")
                    time.sleep(wait)

        print(f"[{self.site_id}] {context} failed after 3 attempts: {last_error}")
        return None

    def _content_disposition_filename(self, url):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skLI",
            "--connect-timeout",
            "10",
            "--max-time",
            "20",
            "-A",
            self.USER_AGENT,
            url,
        ]
        for attempt in range(1, 4):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=25)
                headers = (result.stdout or b"").decode("utf-8", errors="replace")
                if result.returncode != 0:
                    raise RuntimeError(f"curl exit {result.returncode}")
                match = re.search(
                    r"Content-Disposition:[^\n]*filename\\*=UTF-8''([^;\r\n]+)",
                    headers,
                    re.I,
                )
                if match:
                    return unquote(match.group(1).strip().strip('"'))
                match = re.search(
                    r"Content-Disposition:[^\n]*filename=\"?([^\";\r\n]+)",
                    headers,
                    re.I,
                )
                if match:
                    return unquote(match.group(1).strip())
                return None
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(
                    f"[{self.site_id}] PDF header fetch failed "
                    f"(attempt {attempt}/3): {exc}"
                )
                if attempt < 3:
                    time.sleep(self.BACKOFF_SECONDS[attempt - 1])
        return None

    def _split_curl_output(self, raw, fallback_url):
        marker = ("\n" + self.CURL_MARKER).encode("ascii")
        pos = raw.rfind(marker)
        if pos == -1:
            return raw, "", fallback_url
        body = raw[:pos]
        meta = raw[pos + len(marker) :].decode("utf-8", errors="replace").strip()
        if "\t" not in meta:
            return body, "", fallback_url
        code, effective_url = meta.split("\t", 1)
        return body, code.strip(), effective_url.strip() or fallback_url

    def _make_soup(self, raw, context="HTML"):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                last_exc = exc
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        print(f"[{self.site_id}] all BeautifulSoup parsers failed for {context}: {last_exc}")
        return None

    def _clean_html(self, value):
        if value is None:
            return ""
        text = str(value)
        if "<" in text and ">" in text:
            soup = self._make_soup(text, context="html text")
            if soup is not None:
                text = soup.get_text(" ", strip=True)
            else:
                text = re.sub(r"<[^>]+>", " ", text)
        return self._clean_text(text)

    def _clean_text(self, value):
        if value is None:
            return ""
        if hasattr(value, "get_text"):
            value = value.get_text(" ", strip=True)
        text = unescape(str(value))
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _iso_date(self, value):
        value = self._clean_text(value)
        if not value:
            return None
        match = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
        if match:
            return match.group(0)
        match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", value)
        if match:
            day, month, year = match.groups()
            return f"{year}-{int(month):02d}-{int(day):02d}"
        return None

    def _post_number(self, document_id, publication_id, api_id):
        for value in (document_id, publication_id):
            if value is not None and str(value).isdigit():
                return str(value)
        match = re.search(r"(\d+)$", str(api_id or ""))
        if match:
            return match.group(1)
        return self._clean_text(api_id) or None

    def _pick_language_record(self, records, lang):
        for record in records or []:
            if isinstance(record, dict) and record.get("lang") == lang:
                return record
        for record in records or []:
            if not isinstance(record, dict):
                continue
            language = (
                ((record.get("content") or {}).get("document") or {})
                .get("metadata", {})
                .get("language", {})
            )
            if language.get("locale") == lang:
                return record
        return records[0] if records else None

    def _as_string_list(self, value):
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            return [self._clean_text(v) for v in value if self._clean_text(v)]
        text = self._clean_text(value)
        if not text:
            return []
        return [p.strip() for p in re.split(r"[;,]", text) if p.strip()]

    def _join_unique(self, values, sep=","):
        seen = set()
        out = []
        for value in values:
            text = self._clean_html(value)
            if text and text not in seen:
                seen.add(text)
                out.append(text)
        return sep.join(out) if out else None

    def _filename_from_url(self, url):
        if not url:
            return None
        tail = urlparse(url).path.rstrip("/").split("/")[-1]
        tail = unquote(tail.split("?")[0].split("#")[0])
        match = re.search(r"(.+?\.pdf)", tail, re.I)
        if match:
            return match.group(1)
        return tail or None
