# -*- coding: utf-8 -*-
"""Crawler for swisspeace publications.

Source: https://www.swisspeace.ch/publications
The public page is a React/Silverstripe shell.  Its page JSON exposes an
ArticleListElement whose ``filterUrl`` points at the real list API.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import unquote, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class SwisspeaceChPublicationsCrawler(BaseCrawler):
    site_id = "swisspeace-ch-publications"
    site_name = "Custom: swisspeace-ch-publications"
    base_url = "https://www.swisspeace.ch"

    START_URL = "https://www.swisspeace.ch/publications"
    INITIAL_API = "https://www.swisspeace.ch/api/data/initial/publications"
    DETAIL_API_PREFIX = "https://www.swisspeace.ch/api/data/url/"

    PAGE_SIZE = 10
    SAFETY_PAGE_CAP = 200
    WALL_CLOCK_BUDGET_SECONDS = 25 * 60
    BACKOFF_SECONDS = (1, 3, 9)
    CURL_TIMEOUT = 45
    MIN_ABSTRACT_CHARS = 50
    _CURL_META_MARKER = "__SWISSPEACE_CH_PUBLICATIONS_CURL_META__:"

    PROGRAM_FILTER_ID = 1
    PUBLICATION_TYPE_FILTER_ID = 4

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay
        self._tag_titles = {}
        self._program_tag_ids = set()
        self._publication_type_tag_ids = set()

    def crawl(self, limit=None):
        """Crawl swisspeace publication records through the JSON APIs."""
        saved = 0
        page = 1
        seen_urls = set()
        started_at = time.time()
        limit_label = str(limit) if limit is not None else "inf"

        config = self._discover_list_config()
        list_endpoint = config["list_endpoint"]
        print(f"[{self.site_id}] list endpoint: {list_endpoint}")

        while page <= self.SAFETY_PAGE_CAP:
            if limit is not None and saved >= limit:
                break
            if self._budget_nearly_exhausted(started_at):
                print(f"[{self.site_id}] time budget nearly exhausted; stopping cleanly")
                break
            if page == 1 or page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            offset = (page - 1) * self.PAGE_SIZE
            list_data = self._fetch_list_page(config, offset)
            if list_data is None:
                print(f"[{self.site_id}] list page {page} failed; stopping")
                break

            records = list_data.get("articles") if isinstance(list_data, dict) else None
            if not records:
                print(f"[{self.site_id}] list page {page} returned no records; stopping")
                break

            new_on_page = 0
            for idx, record in enumerate(records, start=1):
                if limit is not None and saved >= limit:
                    break
                if self._budget_nearly_exhausted(started_at):
                    print(f"[{self.site_id}] time budget nearly exhausted; stopping cleanly")
                    return saved

                item_label = f"page {page} item {idx}"
                try:
                    detail_url = self._record_detail_url(record)
                    dedupe_url = detail_url or self._record_external_url(record)
                    if not dedupe_url:
                        dedupe_url = f"{self.base_url}/articles/{record.get('id')}"
                    if dedupe_url in seen_urls:
                        continue
                    seen_urls.add(dedupe_url)
                    new_on_page += 1

                    time.sleep(self.detail_delay)
                    detail = self._fetch_detail(record, detail_url) if detail_url else None
                    parsed = self._parse_record(record, detail, detail_url)

                    abstract = parsed.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    paper = self._to_paper(parsed)
                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {paper['title'][:90]}")
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page} had no unseen URLs; stopping")
                break

            count = self._safe_int(list_data.get("count"))
            returned = len(records)
            if returned == 0:
                break
            if count is not None and offset + returned >= count:
                break
            page += 1

        if page > self.SAFETY_PAGE_CAP:
            print(f"[{self.site_id}] reached safety page cap ({self.SAFETY_PAGE_CAP}); stopping")

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Discovery and API fetching
    # ------------------------------------------------------------------

    def _discover_list_config(self):
        raw, _, _ = self._curl_get_text(
            self.INITIAL_API,
            context="initial page JSON",
            referer=self.START_URL,
            accept="application/json,*/*;q=0.8",
        )
        if not raw:
            raise RuntimeError("initial page JSON fetch failed")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"initial page JSON invalid: {exc}") from exc

        page = data.get("page") if isinstance(data, dict) else {}
        site = data.get("site") if isinstance(data, dict) else {}
        self._tag_titles = self._extract_tag_titles(site)

        list_element = None
        for section in (page.get("sections") or {}).values():
            for element in section or []:
                if isinstance(element, dict) and element.get("className") == "ArticleListElement":
                    list_element = element
                    break
            if list_element:
                break
        if not list_element:
            raise RuntimeError("ArticleListElement not found in page JSON")

        self._populate_filter_tag_sets(list_element)
        filter_url = list_element.get("filterUrl") or "/articles/filter"
        endpoint = urljoin(self.base_url, filter_url)
        page_size = self._safe_int(list_element.get("limit"), self.PAGE_SIZE) or self.PAGE_SIZE
        self.PAGE_SIZE = page_size

        return {
            "list_endpoint": endpoint,
            "formats": list_element.get("formats") or ["Publication"],
            "limit": page_size,
            "excludedTagIds": list_element.get("excludedTagIds") or [42, 36],
            "mandatoryTagIds": list_element.get("tags") or None,
            "raw_list_element": list_element,
        }

    def _fetch_list_page(self, config, offset):
        params = [
            ("o", str(offset)),
            ("limit", str(config.get("limit") or self.PAGE_SIZE)),
        ]
        # Silverstripe's controller expects Axios' default bracketed array
        # encoding.  Plain ``formats=Publication`` produces HTTP 500.
        for value in config.get("formats") or ["Publication"]:
            params.append(("formats[]", value))
        for value in config.get("excludedTagIds") or []:
            params.append(("excludedTagIds[]", str(value)))
        for value in config.get("mandatoryTagIds") or []:
            params.append(("mandatoryTagIds[]", str(value)))
        url = f"{config['list_endpoint']}?{urlencode(params)}"
        raw, _, _ = self._curl_get_text(
            url,
            context=f"list offset {offset}",
            referer=self.START_URL,
            accept="application/json,*/*;q=0.8",
        )
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] invalid list JSON at offset {offset}: {exc}")
            return None
        if not isinstance(data, dict):
            print(f"[{self.site_id}] unexpected list JSON shape at offset {offset}")
            return None
        return data

    def _fetch_detail(self, record, detail_url):
        path = urlparse(detail_url).path.lstrip("/")
        if not path:
            return None
        url = self.DETAIL_API_PREFIX + path
        raw, _, _ = self._curl_get_text(
            url,
            context=f"detail API {record.get('id') or detail_url}",
            referer=self.START_URL,
            accept="application/json,*/*;q=0.8",
        )
        if not raw:
            raise RuntimeError("detail API fetch failed after retries")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"detail JSON invalid: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError("detail JSON has unexpected shape")
        return data

    def _curl_get_text(self, url, context="request", referer=None, accept=None):
        write_out = "\n" + self._CURL_META_MARKER + "%{http_code}\t%{url_effective}"
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
            f"Accept: {accept or 'text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8'}",
            "-H",
            "Accept-Language: en-US,en;q=0.9",
            "-w",
            write_out,
            url,
        ]
        if referer:
            cmd[-1:-1] = ["-H", f"Referer: {referer}"]

        last_error = None
        for attempt, wait in enumerate(self.BACKOFF_SECONDS, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=self.CURL_TIMEOUT + 10)
                text = result.stdout.decode("utf-8", errors="replace")
                body, http_code, effective_url = self._split_curl_meta(text, url)
                if result.returncode == 0 and http_code and 200 <= http_code < 400 and body.strip():
                    return body, effective_url, {"http_code": http_code}
                last_error = (
                    f"curl rc={result.returncode} http={http_code} "
                    f"stderr={result.stderr.decode('utf-8', errors='replace')[:200]}"
                )
            except Exception as exc:
                last_error = str(exc)

            if attempt < len(self.BACKOFF_SECONDS):
                print(
                    f"[{self.site_id}] {context} failed "
                    f"(attempt {attempt}/3): {last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] {context} failed after 3 attempts: {last_error}")
        return None, url, {}

    def _split_curl_meta(self, text, fallback_url):
        if self._CURL_META_MARKER not in text:
            return text, None, fallback_url
        body, meta = text.rsplit(self._CURL_META_MARKER, 1)
        parts = meta.strip().split("\t")
        http_code = self._safe_int(parts[0]) if parts else None
        effective_url = parts[1] if len(parts) > 1 and parts[1] else fallback_url
        return body, http_code, effective_url

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_record(self, record, detail, detail_url):
        source = detail if isinstance(detail, dict) else record
        node_id = self._safe_int(record.get("id") or source.get("id"))
        external_id = str(node_id) if node_id is not None else self._external_id(record, detail_url)
        post_number = str(node_id) if node_id is not None else external_id

        title = self._clean_text(source.get("title") or record.get("title") or "")
        authors = self._clean_authors(source.get("headline") or record.get("headline") or "")
        published_date = self._normalize_date(source.get("publicationDate") or record.get("publicationDate"))
        listed_date = self._normalize_date(record.get("publicationDate"))
        tag_ids = self._as_int_list(source.get("tags") or source.get("tagIds") or record.get("tags") or record.get("tagIds"))
        tag_titles = [self._tag_titles.get(tid, str(tid)) for tid in tag_ids]
        category = self._category_from_tags(tag_ids)
        department = self._department_from_tags(tag_ids)

        links = self._collect_links(record, detail)
        pdf_url = self._first_pdf_url(links)
        if not pdf_url:
            pdf_url = self._first_pdf_url_from_content(detail)
        original_filename = self._filename_from_url(pdf_url)
        doi = self._extract_doi_from_links(links) or self._extract_doi(source.get("citation") or "")

        content_text, content_html, content_links = self._extract_content_text(detail)
        if not pdf_url:
            pdf_url = self._first_pdf_url(content_links)
            original_filename = self._filename_from_url(pdf_url)
        if not doi:
            doi = self._extract_doi_from_links(content_links) or self._extract_doi(content_text)

        abstract = self._choose_abstract(source, content_text)
        journal_raw = source.get("journal") or record.get("journal") or ""
        journal = self._clean_text(journal_raw)
        series, volume, issue = self._extract_series_bits(content_text, journal, category)

        meta_url = detail_url or self._record_external_url(record) or ""
        if meta_url:
            meta_url = urljoin(self.base_url, meta_url)

        metadata = {
            "source": "Silverstripe React JSON APIs",
            "start_url": self.START_URL,
            "list_endpoint": "/articles/filter",
            "detail_endpoint": (self.DETAIL_API_PREFIX + urlparse(detail_url).path.lstrip("/")) if detail_url else None,
            "node_id": node_id,
            "post_number": post_number,
            "className": source.get("className") or record.get("className"),
            "posted_date": record.get("publicationDate"),
            "listed_date": record.get("publicationDate"),
            "originalFilename": original_filename,
            "journal_raw": journal_raw,
            "series": series,
            "volume": volume,
            "issue": issue,
            "tagIds": tag_ids,
            "tag_titles": tag_titles,
            "links": links,
            "content_links": content_links,
            "citation": source.get("citation") or record.get("citation"),
            "downloads": source.get("downloads") or [],
            "partners": source.get("partners") or [],
            "list_record": record,
            "detail_record": detail,
            "content_html": content_html,
        }
        metadata = {k: v for k, v in metadata.items() if v not in (None, "", [], {})}

        return {
            "id": f"{self.site_id}:{external_id}",
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": authors,
            "publisher": "swisspeace" if not journal else "swisspeace",
            "department": department,
            "journal": journal,
            "url": meta_url,
            "pdf_url": pdf_url,
            "keywords": ", ".join(tag_titles) if tag_titles else "",
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": metadata,
        }

    def _to_paper(self, parsed):
        return {
            "id": parsed.get("id"),
            "site_id": self.site_id,
            "external_id": parsed.get("external_id"),
            "post_number": parsed.get("post_number"),
            "title": parsed.get("title"),
            "abstract": parsed.get("abstract"),
            "published_date": parsed.get("published_date"),
            "listed_date": parsed.get("listed_date"),
            "posted_date": parsed.get("posted_date") or parsed.get("listed_date"),
            "authors": parsed.get("authors"),
            "publisher": parsed.get("publisher"),
            "department": parsed.get("department") or parsed.get("publisher"),
            "journal": parsed.get("journal"),
            "url": parsed.get("url"),
            "pdf_url": parsed.get("pdf_url"),
            "keywords": parsed.get("keywords"),
            "category": parsed.get("category"),
            "doi": parsed.get("doi"),
            "original_filename": parsed.get("original_filename"),
            "metadata": json.dumps(parsed.get("metadata") or {}, ensure_ascii=False),
        }

    def _extract_content_text(self, detail):
        if not isinstance(detail, dict):
            return "", "", []

        text_parts = []
        html_parts = []
        links = []
        for element in detail.get("content") or []:
            if not isinstance(element, dict):
                continue
            html = element.get("content")
            if not html:
                continue
            html_parts.append(html)
            soup = self._make_soup(html, context=f"content element {element.get('id')}")
            if soup is None:
                text = self._clean_text(re.sub(r"<[^>]+>", " ", html))
                if text:
                    text_parts.append(text)
                continue

            for a_tag in soup.select("a[href]"):
                href = a_tag.get("href") or ""
                if href:
                    links.append({
                        "title": self._clean_text(a_tag.get_text(" ", strip=True)),
                        "href": urljoin(self.base_url, href),
                    })

            for paragraph in soup.find_all(["p", "li"]):
                classes = paragraph.get("class") or []
                text = self._clean_text(paragraph.get_text(" ", strip=True))
                if not text:
                    continue
                if "rte-font-big" in classes:
                    continue
                if self._looks_like_download_line(text, paragraph):
                    continue
                text_parts.append(text)

        text = self._clean_text(" ".join(text_parts))
        return text, "\n".join(html_parts), links

    def _choose_abstract(self, source, content_text):
        candidates = [
            content_text,
            self._clean_text(source.get("shortDescription") or ""),
        ]
        for candidate in candidates:
            if len(candidate) >= self.MIN_ABSTRACT_CHARS:
                return candidate
        return candidates[0] if candidates else ""

    def _first_pdf_url_from_content(self, detail):
        _, _, links = self._extract_content_text(detail)
        return self._first_pdf_url(links)

    def _collect_links(self, record, detail):
        links = []
        for source in (record, detail):
            if not isinstance(source, dict):
                continue
            for item in source.get("links") or []:
                if isinstance(item, dict) and item.get("href"):
                    links.append({
                        "title": self._clean_text(item.get("title") or item.get("linkLabel") or ""),
                        "href": urljoin(self.base_url, item.get("href")),
                    })
            for item in source.get("downloads") or []:
                if isinstance(item, dict) and item.get("link"):
                    links.append({
                        "title": self._clean_text(item.get("title") or item.get("linkLabel") or ""),
                        "href": urljoin(self.base_url, item.get("link")),
                    })
        return links

    def _first_pdf_url(self, links):
        for link in links or []:
            href = link.get("href") if isinstance(link, dict) else str(link)
            if href and ".pdf" in urlparse(href).path.lower():
                return href
        return None

    def _record_detail_url(self, record):
        link = record.get("link") if isinstance(record, dict) else None
        if not link:
            return None
        return urljoin(self.base_url, link)

    def _record_external_url(self, record):
        for link in (record.get("links") or []) if isinstance(record, dict) else []:
            href = link.get("href") if isinstance(link, dict) else None
            if href:
                return urljoin(self.base_url, href)
        return None

    # ------------------------------------------------------------------
    # Normalization helpers
    # ------------------------------------------------------------------

    def _make_soup(self, html, context="HTML"):
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html or "", parser)
            except Exception as exc:
                print(f"[{self.site_id}] {context}: BeautifulSoup {parser} failed: {exc}")
                continue
        return None

    def _extract_tag_titles(self, site):
        tags = (site or {}).get("tags") or {}
        titles = {}
        for key, value in tags.items():
            tid = self._safe_int(key)
            if tid is None and isinstance(value, dict):
                tid = self._safe_int(value.get("id"))
            if tid is None:
                continue
            title = value.get("title") if isinstance(value, dict) else str(value)
            titles[tid] = self._clean_text(title)
        return titles

    def _populate_filter_tag_sets(self, list_element):
        self._program_tag_ids = set()
        self._publication_type_tag_ids = set()
        for filter_item in list_element.get("filters") or []:
            if not isinstance(filter_item, dict):
                continue
            tag_ids = {tid for tid in (self._safe_int(k) for k in (filter_item.get("tags") or {}).keys()) if tid is not None}
            if filter_item.get("id") == self.PROGRAM_FILTER_ID:
                self._program_tag_ids = tag_ids
            if filter_item.get("id") == self.PUBLICATION_TYPE_FILTER_ID:
                self._publication_type_tag_ids = tag_ids

    def _category_from_tags(self, tag_ids):
        for tid in tag_ids:
            if tid in self._publication_type_tag_ids:
                return self._tag_titles.get(tid, str(tid))
        return "Publication"

    def _department_from_tags(self, tag_ids):
        names = [self._tag_titles.get(tid, str(tid)) for tid in tag_ids if tid in self._program_tag_ids]
        return "; ".join(names) if names else None

    @staticmethod
    def _as_int_list(value):
        items = value if isinstance(value, list) else []
        out = []
        for item in items:
            try:
                out.append(int(item))
            except (TypeError, ValueError):
                continue
        return out

    @staticmethod
    def _safe_int(value, default=None):
        try:
            if value is None or value == "":
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _clean_text(value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _clean_authors(self, value):
        text = self._clean_text(value)
        if not text:
            return ""
        text = re.sub(r"\s+(with|and)\s+", "; ", text, flags=re.IGNORECASE)
        text = text.replace(" & ", "; ")
        text = re.sub(r"\s*,\s*", "; ", text)
        text = re.sub(r"\s*;\s*", "; ", text)
        return text.strip(" ;")

    @staticmethod
    def _normalize_date(value):
        if not value:
            return ""
        text = str(value).strip()
        match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
        if match:
            return match.group(0)
        match = re.search(r"(\d{4})", text)
        if match:
            return f"{match.group(1)}-01-01"
        return ""

    def _looks_like_download_line(self, text, paragraph):
        lowered = text.lower()
        if lowered in {"download", "download in english", "download in albanian", "download in serbian"}:
            return True
        if lowered.startswith("download ") or lowered.startswith("download in "):
            return True
        links = paragraph.find_all("a") if paragraph else []
        if links and all(".pdf" in (a.get("href") or "").lower() for a in links):
            link_text = self._clean_text(" ".join(a.get_text(" ", strip=True) for a in links)).lower()
            if "download" in link_text or len(text) < 80:
                return True
        return False

    @staticmethod
    def _filename_from_url(url):
        if not url:
            return None
        path = urlparse(url).path
        tail = unquote(path.rstrip("/").split("/")[-1])
        if tail and "." in tail and len(tail) <= 240:
            return tail
        return None

    @staticmethod
    def _extract_doi(text):
        if not text:
            return None
        match = re.search(r"\b(10\.\d{4,9}/[^\s\"'<>]+)", text)
        if not match:
            return None
        return match.group(1).rstrip(").,;")

    def _extract_doi_from_links(self, links):
        for link in links or []:
            href = link.get("href") if isinstance(link, dict) else str(link)
            doi = self._extract_doi(href or "")
            if doi:
                return doi
        return None

    def _extract_series_bits(self, content_text, journal, category):
        series = None
        volume = None
        issue = None

        if content_text:
            match = re.search(r"\b(swisspeace\s+(?:Policy Brief|Working Paper|Report|Essential)s?)\s+([0-9]{1,2}\.[0-9]{4})", content_text, re.IGNORECASE)
            if match:
                series = self._clean_text(match.group(1))
                issue = match.group(2)
            elif category and category != "Publication":
                series = category

        raw = journal or ""
        journal_match = re.search(r"\b(\d+)\s*\((\d+)\)", raw)
        if journal_match:
            volume = journal_match.group(1)
            issue = issue or journal_match.group(2)
        elif raw:
            volume_match = re.search(r"\bvol(?:ume)?\.?\s*(\d+)", raw, re.IGNORECASE)
            issue_match = re.search(r"\b(?:no|issue)\.?\s*(\d+)", raw, re.IGNORECASE)
            volume = volume or (volume_match.group(1) if volume_match else None)
            issue = issue or (issue_match.group(1) if issue_match else None)

        return series, volume, issue

    def _external_id(self, record, detail_url):
        if isinstance(record, dict) and record.get("id"):
            return str(record.get("id"))
        url = detail_url or self._record_external_url(record) or ""
        path = urlparse(url).path.strip("/")
        slug = path.split("/")[-1] if path else ""
        return slug or None

    def _budget_nearly_exhausted(self, started_at):
        return (time.time() - started_at) >= (self.WALL_CLOCK_BUDGET_SECONDS - 30)
