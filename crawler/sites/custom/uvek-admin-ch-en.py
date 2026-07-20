# -*- coding: utf-8 -*-
"""Crawler for DETEC/UVEK English press releases.

The current uvek.admin.ch site is a Nuxt app backed by the Swiss
Content Broker news API:

    https://d-nsbc-p.admin.ch/v1/search
    https://d-nsbc-p.admin.ch/v1/languageGroupId/{id}

The public landing page at /en/detec-press-releases renders only the first
50 cards, so full-depth pagination must use the API.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timezone
from html import unescape
from urllib.parse import unquote, urlencode, urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class UvekAdminChEnCrawler(BaseCrawler):
    site_id = "uvek-admin-ch-en"
    site_name = "Custom: uvek-admin-ch-en"
    base_url = "https://www.uvek.admin.ch"

    START_URL = "https://www.uvek.admin.ch/en/detec-press-releases"
    API_BASE = "https://d-nsbc-p.admin.ch/v1"
    SEARCH_URL = f"{API_BASE}/search"
    DETAIL_URL_TPL = f"{API_BASE}/languageGroupId/{{lang_group_id}}"
    PUBLISHER_ID = "8"
    PAGE_SIZE = 50
    SAFETY_PAGE_CAP = 200
    WALL_LIMIT_SECONDS = 25 * 60
    WALL_APPROACH_SECONDS = 24 * 60
    CURL_TIMEOUT = 60
    BACKOFF = (1, 3, 9)
    MIN_ABSTRACT = 50
    PREFERRED_ABSTRACT = 100
    MAX_ABSTRACT = 4000
    CURL_MARKER = "__UVEK_CURL_META__:"

    MONTHS = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }

    CATEGORY_LABELS = {
        "medienmitteilung": "Press release",
        "fremdmitteilung": "External announcement",
        "newsletter": "Newsletter",
        "rede": "Speech",
    }

    def crawl(self, limit=None):
        saved = 0
        page = 0
        start_time = time.time()
        seen_urls = set()
        limit_label = str(limit) if limit is not None else "inf"
        org_map = self._load_organisation_map()
        topic_map = self._load_topic_map()

        while True:
            if limit is not None and saved >= limit:
                break
            if page >= self.SAFETY_PAGE_CAP:
                print(f"[{self.site_id}] safety cap of {self.SAFETY_PAGE_CAP} pages reached")
                break
            if time.time() - start_time >= self.WALL_APPROACH_SECONDS:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget, stopping cleanly")
                break

            page += 1
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            offset = (page - 1) * self.PAGE_SIZE
            items = self._fetch_search_page(offset)
            if not items:
                print(f"[{self.site_id}] page {page}: no records, stopping")
                break

            new_urls_on_page = 0
            for idx, item in enumerate(items):
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time >= self.WALL_APPROACH_SECONDS:
                    print(f"[{self.site_id}] approaching 25-minute wall-clock budget, stopping cleanly")
                    return saved

                item_label = f"page {page} item {idx}"
                try:
                    list_record = self._parse_search_item(item)
                    if not list_record:
                        continue

                    url = list_record["url"]
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    new_urls_on_page += 1

                    if self._delay:
                        time.sleep(self._delay)

                    detail_record = self._fetch_detail_record(list_record["external_id"])
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
                    if abstract_len < self.MIN_ABSTRACT:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({abstract_len} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_label}: {paper['title'][:80]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if new_urls_on_page == 0:
                print(f"[{self.site_id}] page {page}: 0 new records, stopping")
                break

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # API
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
        url = self.SEARCH_URL + "?" + urlencode(params)
        raw = self._curl_text(url, context=f"search offset={offset}")
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except ValueError as exc:
            print(f"[{self.site_id}] search JSON parse failed at offset={offset}: {exc}")
            return []
        items = data.get("items")
        if not isinstance(items, list):
            return []
        return items

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
        if isinstance(data, dict):
            return data
        return None

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
                if raw_id is not None:
                    label = self._clean_text(node.get("label") or node.get("name") or "")
                    acronym = self._clean_text(node.get("acronym") or "")
                    if label:
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
                label = self._clean_text(node.get("label") or "")
                if raw_id is not None and label:
                    topics[str(raw_id)] = label
                walk(node.get("children") or [])

        walk(data if isinstance(data, list) else [])
        return topics

    # ------------------------------------------------------------------
    # Build records
    # ------------------------------------------------------------------

    def _parse_search_item(self, item):
        if not isinstance(item, dict):
            return None
        lang_group_id = self._clean_text(item.get("langGroupId") or "")
        api_id = self._clean_text(item.get("id") or "")
        external_id = lang_group_id or api_id
        if not external_id:
            return None

        title = self._clean_text(item.get("title") or "")
        description = self._clean_html(item.get("description") or "")
        publish_raw = item.get("publishDate") or ""
        listed_date = self._iso_date(publish_raw)
        systemdata = ((item.get("content") or {}).get("systemdata") or {})
        document_id = systemdata.get("documentId")
        publication_id = systemdata.get("publicationId")
        post_number = self._post_number(document_id, publication_id, api_id)
        url = f"{self.base_url}/en/newnsb/{external_id}"

        if not title or not url:
            return None
        return {
            "api_id": api_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": description,
            "listed_date": listed_date,
            "listed_date_raw": publish_raw,
            "url": url,
            "category_raw": item.get("newsCategory"),
            "topics": item.get("topics") or [],
            "publishers": item.get("publishers") or [],
            "co_publishers": item.get("coPublishers") or [],
            "systemdata": systemdata,
        }

    def _build_paper(self, list_record, search_item, detail_record, org_map, topic_map):
        detail = detail_record if isinstance(detail_record, dict) else {}
        content = detail.get("content") or {}
        document = content.get("document") or {}
        metadata = document.get("metadata") or {}
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
            or detail.get("publishDate")
            or list_record.get("listed_date_raw")
        )
        published_date = self._iso_date(published_raw)
        listed_date = list_record.get("listed_date") or self._iso_date(detail.get("publishDate"))
        listed_raw = list_record.get("listed_date_raw") or detail.get("publishDate")

        abstract = self._extract_abstract(
            metadata=metadata,
            document=document,
            fallback=list_record.get("abstract"),
        )
        if len(abstract) < self.PREFERRED_ABSTRACT:
            html_abstract = self._fetch_detail_html_abstract(list_record["url"])
            if len(html_abstract) > len(abstract):
                abstract = html_abstract
        abstract = abstract[: self.MAX_ABSTRACT]

        category_raw = detail.get("newsCategory") or list_record.get("category_raw")
        category = self.CATEGORY_LABELS.get(str(category_raw or ""), category_raw or "Press release")
        topic_ids = self._as_string_list(detail.get("topics") or list_record.get("topics"))
        keywords = self._join_unique([topic_map.get(t, t) for t in topic_ids if t])

        publisher_ids = self._as_string_list(detail.get("publishers") or list_record.get("publishers"))
        co_publisher_ids = self._as_string_list(
            detail.get("coPublishers") or list_record.get("co_publishers")
        )
        publisher = self._join_unique(
            [org_map.get(pid, pid) for pid in publisher_ids + co_publisher_ids if pid]
        )
        department = org_map.get(self.PUBLISHER_ID, "Federal Department of the Environment, Transport, Energy and Communications (DETEC)")

        pdf_url = self._find_pdf_url(detail)
        original_filename = self._filename_from_url(pdf_url)

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
            "location": detail.get("location") or metadata.get("newsLocation"),
            "source_list_url": self.START_URL,
            "source_api": self.SEARCH_URL,
            "detail_api": self.DETAIL_URL_TPL.format(lang_group_id=list_record.get("external_id")),
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

    def _extract_abstract(self, metadata, document, fallback=""):
        parts = []
        for key in ("description", "metaDescription", "openGraphDescription"):
            text = self._clean_html(metadata.get(key) or "")
            if text:
                parts.append(text)
                break
        parts.extend(self._extract_component_texts(document.get("content") or []))
        if fallback:
            parts.append(self._clean_html(fallback))
        return self._join_sentences(parts)

    def _extract_component_texts(self, value):
        parts = []

        def walk(obj):
            if isinstance(obj, dict):
                content = obj.get("content")
                if isinstance(content, dict):
                    for key in ("title", "text", "lead", "description", "caption"):
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

    def _fetch_detail_html_abstract(self, url):
        raw = self._curl_text(url, context="detail html")
        if not raw:
            return ""
        soup = self._make_soup(raw, context="detail html")
        if soup is None:
            return ""
        selectors = [
            "main#main-content section.hero p.hero__description",
            "main#main-content section.section--default p.font--regular",
            "main#main-content p",
        ]
        parts = []
        for selector in selectors:
            for el in soup.select(selector):
                text = self._clean_text(el)
                if len(text) >= 40:
                    parts.append(text)
            if parts:
                break
        return self._join_sentences(parts)[: self.MAX_ABSTRACT]

    def _find_pdf_url(self, detail):
        candidates = []

        def add_url(raw):
            if not raw:
                return
            raw = unescape(str(raw).strip())
            if not raw:
                return
            match = re.search(r"https?://[^\s\"'<>]+?\.pdf(?:[^\s\"'<>]*)?", raw, re.I)
            if match:
                candidates.append(match.group(0))
                return
            if ".pdf" in raw.lower():
                candidates.append(raw)

        def walk(obj):
            if isinstance(obj, dict):
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
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        return None

    def _clean_html(self, value):
        if not value:
            return ""
        text = str(value)
        if "<" in text and ">" in text:
            soup = self._make_soup(text, context="html text")
            if soup is not None:
                text = soup.get_text(" ", strip=True)
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
        match = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", value)
        if match:
            day, month_name, year = match.groups()
            month = self.MONTHS.get(month_name.lower())
            if month:
                return f"{year}-{month:02d}-{int(day):02d}"
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
        for record in records:
            if isinstance(record, dict) and record.get("lang") == lang:
                return record
        for record in records:
            if isinstance(record, dict):
                language = ((record.get("content") or {}).get("document") or {}).get("metadata", {}).get("language") or {}
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

    def _join_unique(self, values, sep=", "):
        seen = set()
        out = []
        for value in values:
            text = self._clean_text(value)
            if text and text not in seen:
                seen.add(text)
                out.append(text)
        return sep.join(out) if out else None

    def _join_sentences(self, values):
        seen = set()
        out = []
        for value in values:
            text = self._clean_html(value)
            if not text or text in seen:
                continue
            seen.add(text)
            out.append(text)
        return " ".join(out)

    def _filename_from_url(self, url):
        if not url:
            return None
        tail = url.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
        tail = unquote(tail)
        match = re.search(r"(.+?\.pdf)", tail, re.I)
        if match:
            return match.group(1)
        return tail or None
