# -*- coding: utf-8 -*-
"""Crawler for VBS/DDPS English press releases.

The public page is a Nuxt app backed by the Swiss News Service content broker:

    https://d-nsbc-p.admin.ch/v1/search
    https://d-nsbc-p.admin.ch/v1/languageGroupId/{id}

The search endpoint returns both newer CONTENT_HUB records and older ONSB
records, so the parser handles both shapes.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import unquote, urlencode, urljoin

from crawler.base_crawler import BaseCrawler


class VbsAdminChEnCrawler(BaseCrawler):
    site_id = "vbs-admin-ch-en"
    site_name = "Custom: vbs-admin-ch-en"
    base_url = "https://www.vbs.admin.ch"

    START_URL = (
        "https://www.vbs.admin.ch/en/newnsb?"
        "publisherIDs=5&topicIDs=all&newsCategoryIDs=medienmitteilung"
        "&sort=dateDecreasing&display=list&start_date=2024-08-29"
        "&end_date=2025-08-29"
    )
    API_BASE = "https://d-nsbc-p.admin.ch/v1"
    SEARCH_URL = f"{API_BASE}/search"
    DETAIL_URL_TPL = f"{API_BASE}/languageGroupId/{{lang_group_id}}"
    PUBLISHER_ID = "5"
    NEWS_CATEGORY_ID = "medienmitteilung"
    START_DATE = "2024-08-29T00:00:00.000Z"
    END_DATE = "2025-08-29T23:59:59.999Z"
    PAGE_SIZE = 50
    SAFETY_PAGE_CAP = 200
    WALL_LIMIT_SECONDS = 25 * 60
    WALL_APPROACH_SECONDS = 24 * 60
    CURL_TIMEOUT = 60
    BACKOFF = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 50
    MAX_ABSTRACT_CHARS = 5000
    CURL_MARKER = "__VBS_CURL_META__:"

    CATEGORY_LABELS = {
        "medienmitteilung": "Press release",
        "fremdmitteilung": "External announcement",
        "newsletter": "Newsletter",
        "rede": "Speech",
    }

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls = set()
        start_time = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"
        org_map = self._load_organisation_map()
        topic_map = self._load_topic_map()

        while True:
            if limit is not None and saved >= limit:
                break
            if page > self.SAFETY_PAGE_CAP:
                print(f"[{self.site_id}] safety cap of {self.SAFETY_PAGE_CAP} pages reached")
                break
            if time.time() - start_time >= self.WALL_APPROACH_SECONDS:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget, stopping cleanly")
                break

            if page == 1 or page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            offset = (page - 1) * self.PAGE_SIZE
            data = self._fetch_search_page(offset)
            items = data.get("items") if isinstance(data, dict) else []
            if not items:
                print(f"[{self.site_id}] page {page}: no records, stopping")
                break

            new_urls_on_page = 0
            for index, item in enumerate(items):
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time >= self.WALL_APPROACH_SECONDS:
                    print(f"[{self.site_id}] approaching 25-minute wall-clock budget, stopping cleanly")
                    return saved

                item_label = self._item_label(page, index, item)
                try:
                    list_record = self._parse_list_item(item)
                    if not list_record:
                        continue

                    url = list_record.get("url") or ""
                    if not url:
                        print(f"[{self.site_id}] item {item_label} skipped: missing detail URL")
                        continue
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    new_urls_on_page += 1

                    if self._delay:
                        time.sleep(self._delay)

                    detail_record = self._fetch_detail_record(list_record.get("lang_group_id"))
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
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:80]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if new_urls_on_page == 0:
                print(f"[{self.site_id}] page {page}: 0 new records, stopping")
                break

            total = data.get("pageResults") if isinstance(data, dict) else None
            if isinstance(total, int) and offset + len(items) >= total:
                break
            if len(items) < self.PAGE_SIZE:
                break

            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def _fetch_search_page(self, offset):
        params = [
            ("languages", "en"),
            ("newsKinds", "CONTENT_HUB"),
            ("newsKinds", "ONSB"),
            ("publisherIDs", self.PUBLISHER_ID),
            ("newsCategoryIDs", self.NEWS_CATEGORY_ID),
            ("start_date", self.START_DATE),
            ("end_date", self.END_DATE),
            ("offset", str(offset)),
            ("limit", str(self.PAGE_SIZE)),
            ("sort", "DESC"),
        ]
        url = self.SEARCH_URL + "?" + urlencode(params)
        raw = self._curl_text(url, context=f"search offset={offset}")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError as exc:
            print(f"[{self.site_id}] search JSON parse failed at offset={offset}: {exc}")
            return {}
        return data if isinstance(data, dict) else {}

    def _fetch_detail_record(self, lang_group_id):
        if not lang_group_id:
            return None
        url = self.DETAIL_URL_TPL.format(lang_group_id=lang_group_id)
        raw = self._curl_text(url, context=f"detail {lang_group_id}")
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
                    name = label
                    if acronym and acronym.lower() not in label.lower():
                        name = f"{label} ({acronym})"
                    orgs[str(raw_id)] = name
                walk(node.get("children") or [])

        walk(data if isinstance(data, list) else [])
        return orgs

    def _load_topic_map(self):
        raw = self._curl_text(f"{self.API_BASE}/topicsSubscriber?language=en", context="topics")
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
                label = self._clean_text(node.get("label") or node.get("name") or "")
                if raw_id is not None and label:
                    topics[str(raw_id)] = label
                walk(node.get("children") or [])

        walk(data if isinstance(data, list) else [])
        return topics

    # ------------------------------------------------------------------
    # Build records
    # ------------------------------------------------------------------

    def _parse_list_item(self, item):
        if not isinstance(item, dict):
            return None

        item_id = self._clean_text(item.get("id") or "")
        lang_group_id = self._clean_text(item.get("langGroupId") or "")
        if not lang_group_id and item_id:
            lang_group_id = item_id
        if not item_id and not lang_group_id:
            return None

        content = item.get("content") if isinstance(item.get("content"), dict) else {}
        systemdata = content.get("systemdata") or {}
        legacy_content = content.get("content") if isinstance(content.get("content"), dict) else {}
        metadata = content.get("metadata") or {}

        title = self._clean_text(
            item.get("title")
            or metadata.get("title")
            or metadata.get("metaTitle")
            or legacy_content.get("title")
            or systemdata.get("title")
        )
        if not title:
            return None

        listed_raw = item.get("publishDate") or content.get("publicationDate") or ""
        listed_date = self._iso_date(listed_raw)
        document_id = (
            systemdata.get("documentId")
            or content.get("id")
            or self._first_number(item_id)
        )
        publication_id = systemdata.get("publicationId")
        post_number = self._post_number(document_id, publication_id, item_id, lang_group_id)

        return {
            "api_id": item_id,
            "external_id": item_id or lang_group_id,
            "lang_group_id": lang_group_id,
            "post_number": post_number,
            "title": title,
            "url": f"{self.base_url}/en/newnsb/{lang_group_id}",
            "listed_date": listed_date,
            "listed_date_raw": listed_raw,
            "kind": item.get("kind"),
            "category_raw": item.get("newsCategory") or metadata.get("gdNewsCategory") or content.get("type"),
            "publishers": self._as_string_list(item.get("publishers")),
            "co_publishers": self._as_string_list(item.get("coPublishers")),
            "topics": self._as_string_list(item.get("topics")),
            "tags": item.get("tags") or [],
            "co_publisher_tags": item.get("coPublisherTags") or [],
        }

    def _build_paper(self, list_record, search_item, detail_record, org_map, topic_map):
        detail = detail_record if isinstance(detail_record, dict) else {}
        kind = detail.get("kind") or list_record.get("kind") or ""
        content = detail.get("content") if isinstance(detail.get("content"), dict) else {}

        if kind == "ONSB" or "publicationDate" in content:
            return self._build_onsb_paper(list_record, search_item, detail, org_map, topic_map)
        return self._build_content_hub_paper(list_record, search_item, detail, org_map, topic_map)

    def _build_content_hub_paper(self, list_record, search_item, detail, org_map, topic_map):
        content = detail.get("content") or {}
        document = content.get("document") or {}
        metadata = document.get("metadata") or (content.get("metadata") or {})
        systemdata = document.get("systemdata") or (content.get("systemdata") or {})

        title = self._clean_text(
            metadata.get("title")
            or metadata.get("metaTitle")
            or detail.get("title")
            or list_record.get("title")
            or systemdata.get("title")
        )
        if not title:
            return None

        published_raw = (
            metadata.get("announcementDate")
            or systemdata.get("visiblePublicationDate")
            or systemdata.get("significantPublicationDate")
            or detail.get("publishDate")
            or list_record.get("listed_date_raw")
        )
        published_date = self._iso_date(published_raw)
        listed_raw = list_record.get("listed_date_raw") or detail.get("publishDate")
        listed_date = list_record.get("listed_date") or self._iso_date(listed_raw)

        abstract = self._join_sentences(
            [
                self._clean_html(metadata.get("description") or ""),
                self._clean_html(metadata.get("metaDescription") or ""),
                self._clean_html(detail.get("description") or ""),
                *self._extract_component_texts(document.get("content") or []),
                *[self._clean_html(x) for x in detail.get("text") or []],
            ]
        )[: self.MAX_ABSTRACT_CHARS]

        topic_ids = self._as_string_list(detail.get("topics") or list_record.get("topics"))
        keywords = self._join_keywords_unique([topic_map.get(t, t) for t in topic_ids if t])
        publisher_ids = self._as_string_list(detail.get("publishers") or list_record.get("publishers"))
        co_publisher_ids = self._as_string_list(
            detail.get("coPublishers") or list_record.get("co_publishers")
        )
        publisher = self._join_unique(
            [org_map.get(pid, pid) for pid in publisher_ids + co_publisher_ids if pid]
        )
        department = org_map.get(
            self.PUBLISHER_ID,
            "Federal Department of Defence, Civil Protection and Sport (DDPS)",
        )

        pdf_url = self._find_pdf_url(detail)
        original_filename = self._filename_from_url(pdf_url)
        document_id = systemdata.get("documentId")
        publication_id = systemdata.get("publicationId")
        post_number = list_record.get("post_number") or self._post_number(
            document_id,
            publication_id,
            detail.get("id") or list_record.get("api_id"),
            list_record.get("lang_group_id"),
        )
        category_raw = detail.get("newsCategory") or list_record.get("category_raw")
        category = self.CATEGORY_LABELS.get(str(category_raw or ""), category_raw or "Press release")

        raw_metadata = {
            "posted_date": listed_raw,
            "listed_date": listed_date,
            "published_date_raw": published_raw,
            "originalFilename": original_filename,
            "journal_raw": metadata.get("journal") or metadata.get("journal_raw"),
            "series": metadata.get("series"),
            "volume": metadata.get("volume"),
            "issue": metadata.get("issue"),
            "node_id": document_id,
            "documentId": document_id,
            "publicationId": publication_id,
            "api_id": detail.get("id") or list_record.get("api_id"),
            "langGroupId": list_record.get("lang_group_id"),
            "post_number": post_number,
            "kind": detail.get("kind") or list_record.get("kind"),
            "newsCategory": category_raw,
            "publisherIDs": publisher_ids,
            "coPublisherIDs": co_publisher_ids,
            "topicIDs": topic_ids,
            "tags": detail.get("tags") or list_record.get("tags"),
            "coPublisherTags": detail.get("coPublisherTags") or list_record.get("co_publisher_tags"),
            "location": detail.get("location") or metadata.get("newsLocation"),
            "source_list_url": self.START_URL,
            "source_api": self.SEARCH_URL,
            "detail_api": self.DETAIL_URL_TPL.format(lang_group_id=list_record.get("lang_group_id")),
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
            "url": list_record.get("url"),
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": metadata.get("doi"),
            "original_filename": original_filename,
            "metadata": json.dumps(raw_metadata, ensure_ascii=False),
        }

    def _build_onsb_paper(self, list_record, search_item, detail, org_map, topic_map):
        content = detail.get("content") or {}
        message = content.get("content") if isinstance(content.get("content"), dict) else {}

        title = self._clean_text(message.get("title") or detail.get("title") or list_record.get("title"))
        if not title:
            return None

        listed_raw = list_record.get("listed_date_raw") or detail.get("publishDate") or content.get("publicationDate")
        listed_date = list_record.get("listed_date") or self._iso_date(listed_raw)
        published_raw = content.get("publicationDate") or listed_raw
        published_date = self._iso_date(published_raw)
        native_id = content.get("id") or self._first_number(detail.get("id") or list_record.get("api_id"))
        post_number = str(native_id) if native_id is not None and str(native_id).strip() else list_record.get("post_number")

        lead = self._clean_html(message.get("lead") or detail.get("description") or "")
        body = self._clean_html(message.get("text") or "")
        detail_text = self._join_sentences([self._clean_html(x) for x in detail.get("text") or []])
        abstract = self._join_sentences([lead, body, detail_text])[: self.MAX_ABSTRACT_CHARS]

        originators = content.get("originators") if isinstance(content.get("originators"), list) else []
        departments = content.get("departments") if isinstance(content.get("departments"), list) else []
        originator_names = [self._clean_text(x.get("name") or "") for x in originators if isinstance(x, dict)]
        department_names = [self._clean_text(x.get("name") or "") for x in departments if isinstance(x, dict)]
        publisher = self._join_unique(originator_names + department_names)
        department = self._join_unique(department_names) or org_map.get(
            self.PUBLISHER_ID,
            "Federal Department of Defence, Civil Protection and Sport (DDPS)",
        )

        keyword_names = []
        for kw in content.get("keywords") or []:
            if isinstance(kw, dict):
                keyword_names.append(self._clean_text(kw.get("name") or kw.get("id") or ""))
        for topic in content.get("topics") or []:
            if isinstance(topic, dict):
                keyword_names.append(self._clean_text(topic.get("name") or topic.get("id") or ""))
        for topic_id in self._as_string_list(detail.get("topics") or list_record.get("topics")):
            keyword_names.append(topic_map.get(topic_id, topic_id))
        keywords = self._join_keywords_unique(keyword_names)

        category_raw = detail.get("newsCategory") or list_record.get("category_raw") or content.get("type")
        category = self.CATEGORY_LABELS.get(str(category_raw or ""), category_raw or "Press release")
        pdf_url = self._find_pdf_url(detail)
        original_filename = self._filename_from_url(pdf_url)

        raw_metadata = {
            "posted_date": listed_raw,
            "listed_date": listed_date,
            "published_date_raw": published_raw,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "node_id": native_id,
            "message_id": native_id,
            "api_id": detail.get("id") or list_record.get("api_id"),
            "langGroupId": list_record.get("lang_group_id"),
            "post_number": post_number,
            "kind": detail.get("kind") or list_record.get("kind"),
            "newsCategory": category_raw,
            "publisherIDs": self._as_string_list(detail.get("publishers") or list_record.get("publishers")),
            "coPublisherIDs": self._as_string_list(detail.get("coPublishers") or list_record.get("co_publishers")),
            "topicIDs": self._as_string_list(detail.get("topics") or list_record.get("topics")),
            "tags": detail.get("tags") or list_record.get("tags"),
            "coPublisherTags": detail.get("coPublisherTags") or list_record.get("co_publisher_tags"),
            "originators": originators,
            "departments": departments,
            "links": message.get("links") or [],
            "attachments": message.get("attachments") or [],
            "contact": message.get("contact"),
            "location": detail.get("location") or message.get("city"),
            "source_list_url": self.START_URL,
            "source_api": self.SEARCH_URL,
            "detail_api": self.DETAIL_URL_TPL.format(lang_group_id=list_record.get("lang_group_id")),
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
            "publisher": publisher,
            "department": department,
            "journal": None,
            "url": list_record.get("url"),
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(raw_metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------

    def _extract_component_texts(self, value):
        parts = []

        def walk(obj):
            if isinstance(obj, dict):
                content = obj.get("content")
                if isinstance(content, dict):
                    for key in ("title", "label", "text", "lead", "description", "caption"):
                        text = self._clean_html(content.get(key) or "")
                        if text:
                            parts.append(text)
                containers = obj.get("containers")
                if isinstance(containers, dict):
                    for children in containers.values():
                        walk(children)
                nested = obj.get("nestedContent")
                if nested:
                    walk(nested)
            elif isinstance(obj, list):
                for child in obj:
                    walk(child)

        walk(value)
        return parts

    def _find_pdf_url(self, detail):
        candidates = []

        def add_url(raw):
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
                fmt = str(obj.get("format") or obj.get("mimeType") or obj.get("type") or "").lower()
                if "pdf" in fmt:
                    add_url(obj.get("uri") or obj.get("url") or obj.get("href"))
                asset = obj.get("asset")
                if isinstance(asset, dict):
                    mime = str(asset.get("mimeType") or "").lower()
                    if "pdf" in mime:
                        add_url(asset.get("url") or asset.get("uri"))
                for key, value in obj.items():
                    if key in ("url", "uri", "href", "originalUrl"):
                        add_url(value)
                    elif key == "text" and isinstance(value, str):
                        for href in self._hrefs_from_html(value):
                            add_url(href)
                    walk(value)
            elif isinstance(obj, list):
                for child in obj:
                    walk(child)
            elif isinstance(obj, str) and ".pdf" in obj.lower():
                for href in self._hrefs_from_html(obj):
                    add_url(href)
                add_url(obj)

        walk(detail)
        for candidate in candidates:
            cleaned = self._normalize_pdf_url(candidate)
            if cleaned:
                return cleaned
        return None

    def _hrefs_from_html(self, raw):
        soup = self._make_soup(raw, context="HTML fragment")
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

    # ------------------------------------------------------------------
    # Network / parsing utilities
    # ------------------------------------------------------------------

    def _curl_text(self, url, context="request"):
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
            "Accept: application/json,text/html,application/xhtml+xml,*/*;q=0.8",
            "-H",
            "Accept-Language: en-GB,en;q=0.9",
            "-w",
            "\n" + self.CURL_MARKER + "%{http_code}\t%{url_effective}",
            url,
        ]
        last_err = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.CURL_TIMEOUT + 10,
                )
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
                last_err = str(exc)
                print(f"[{self.site_id}] {context} curl attempt {attempt}/3: {last_err}")
                if attempt < 3:
                    wait = self.BACKOFF[attempt - 1]
                    print(f"[{self.site_id}] retrying in {wait}s...")
                    time.sleep(wait)
        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_err}")
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
        try:
            from bs4 import BeautifulSoup
        except Exception as exc:
            print(f"[{self.site_id}] BeautifulSoup unavailable for {context}: {exc}")
            return None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        return None

    def _clean_html(self, raw):
        if raw is None:
            return ""
        text = str(raw)
        if "<" not in text and ">" not in text:
            return self._clean_text(text)
        soup = self._make_soup(text, context="HTML text")
        if soup is None:
            text = re.sub(r"<[^>]+>", " ", text)
            return self._clean_text(unescape(text))
        return self._clean_text(soup.get_text(" ", strip=True))

    def _clean_text(self, value):
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return re.sub(r"\s+", " ", unescape(value)).strip()

    def _join_sentences(self, parts):
        seen = set()
        out = []
        for part in parts:
            text = self._clean_text(part)
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
        return " ".join(out).strip()

    def _join_unique(self, values):
        seen = set()
        out = []
        for value in values:
            text = self._clean_text(value)
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
        return "; ".join(out) if out else None

    def _join_keywords_unique(self, values):
        seen = set()
        out = []
        for value in values:
            text = self._clean_text(value)
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
        return ", ".join(out) if out else None

    def _as_string_list(self, value):
        if value is None:
            return []
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, tuple):
            return [str(v).strip() for v in value if str(v).strip()]
        text = str(value).strip()
        return [text] if text else []

    def _pick_language_record(self, records, language):
        fallback = None
        for record in records:
            if not isinstance(record, dict):
                continue
            if fallback is None:
                fallback = record
            if record.get("lang") == language:
                return record
            content = record.get("content") if isinstance(record.get("content"), dict) else {}
            if content.get("language") == language:
                return record
            metadata = (content.get("document") or {}).get("metadata") if isinstance(content.get("document"), dict) else {}
            lang_meta = metadata.get("language") if isinstance(metadata, dict) else {}
            if isinstance(lang_meta, dict) and lang_meta.get("locale") == language:
                return record
        return fallback

    def _iso_date(self, raw):
        if raw is None:
            return None
        text = str(raw).strip()
        if not text:
            return None
        match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
        if match:
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
        if match:
            return f"{match.group(3)}-{int(match.group(2)):02d}-{int(match.group(1)):02d}"
        return None

    def _first_number(self, value):
        if value is None:
            return None
        match = re.search(r"\d+", str(value))
        return match.group(0) if match else None

    def _post_number(self, *values):
        for value in values:
            number = self._first_number(value)
            if number:
                return str(number)
        for value in values:
            text = self._clean_text(value)
            if text:
                return text
        return None

    def _filename_from_url(self, url):
        if not url:
            return None
        path = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        tail = unquote(path.rsplit("/", 1)[-1])
        if tail and "." in tail and len(tail) <= 200:
            return tail
        return None

    def _item_label(self, page, index, item):
        item_id = None
        if isinstance(item, dict):
            item_id = item.get("id") or item.get("langGroupId")
        return f"page {page} item {index} {item_id or ''}".strip()
