# -*- coding: utf-8 -*-
"""Crawler for RCSI Repository (Figshare-backed).

Starting URL:
    https://repository.rcsi.com/

Discovery notes:
    The repository front door currently returns an AWS WAF JavaScript
    challenge to plain curl. Public records are available through Figshare's
    API instead:

      List   GET https://api.figshare.com/v2/articles?group=25524...
      Detail GET https://api.figshare.com/v2/articles/{article_id}

    ``group=25524`` is the RCSI repository group. The detail API provides the
    real abstract, authors, custom fields, files, dates, and canonical
    repository.rcsi.com detail URL.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from html import unescape
from urllib.parse import unquote, urlencode, urlparse

from crawler.base_crawler import BaseCrawler


class RepositoryRcsiComCrawler(BaseCrawler):
    site_id = "repository-rcsi-com"
    site_name = "Custom: repository-rcsi-com"
    base_url = "https://repository.rcsi.com"

    API_BASE = "https://api.figshare.com/v2"
    LIST_API = API_BASE + "/articles"
    DETAIL_API = API_BASE + "/articles/{article_id}"
    GROUP_ID = "25524"

    PAGE_SIZE = 100
    SAFETY_PAGE_CAP = 200
    WALL_CLOCK_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    CURL_TIMEOUT = 45
    BACKOFF_SECONDS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 50
    _CURL_META_MARKER = "__REPOSITORY_RCSI_COM_CURL_META__:"

    MONTHS = {
        "jan": 1,
        "january": 1,
        "feb": 2,
        "february": 2,
        "mar": 3,
        "march": 3,
        "apr": 4,
        "april": 4,
        "may": 5,
        "jun": 6,
        "june": 6,
        "jul": 7,
        "july": 7,
        "aug": 8,
        "august": 8,
        "sep": 9,
        "sept": 9,
        "september": 9,
        "oct": 10,
        "october": 10,
        "nov": 11,
        "november": 11,
        "dec": 12,
        "december": 12,
    }

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    def crawl(self, limit=None):
        """Crawl RCSI Repository records from Figshare list/detail APIs."""
        saved = 0
        page = 1
        started_at = time.time()
        seen_urls = set()
        limit_or_inf = str(limit) if limit is not None else "inf"

        print(
            f"[{self.site_id}] list endpoint: {self.LIST_API} "
            f"group={self.GROUP_ID}"
        )

        while page <= self.SAFETY_PAGE_CAP:
            if limit is not None and saved >= limit:
                break
            if self._budget_nearly_exhausted(started_at):
                print(f"[{self.site_id}] time budget nearly exhausted; stopping cleanly")
                break
            if page == 1 or page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            items = self._fetch_list_page(page)
            if items is None:
                print(f"[{self.site_id}] page {page}: list fetch failed; stopping")
                break
            if not items:
                print(f"[{self.site_id}] page {page}: no records. Done.")
                break

            new_items = []
            for item in items:
                detail_url = self._record_url(item)
                if not detail_url:
                    continue
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_items.append(item)

            if not new_items:
                print(f"[{self.site_id}] page {page}: no unseen URLs. Pagination end.")
                break

            for idx, item in enumerate(new_items, start=1):
                if limit is not None and saved >= limit:
                    break
                if self._budget_nearly_exhausted(started_at):
                    print(f"[{self.site_id}] time budget nearly exhausted; stopping cleanly")
                    return saved

                article_id = str(item.get("id") or "").strip()
                item_label = article_id or f"page {page} item {idx}"
                try:
                    if not article_id:
                        continue

                    time.sleep(self.detail_delay)
                    detail = self._fetch_detail(article_id)
                    if detail is None:
                        raise RuntimeError("detail API fetch failed after retries")

                    paper = self._build_paper(item, detail)
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {paper['title'][:90]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            page += 1

        if page > self.SAFETY_PAGE_CAP:
            print(f"[{self.site_id}] reached safety page cap ({self.SAFETY_PAGE_CAP})")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page):
        offset = (page - 1) * self.PAGE_SIZE
        params = {
            "group": self.GROUP_ID,
            "limit": str(self.PAGE_SIZE),
            "offset": str(offset),
            "order": "published_date",
            "order_direction": "desc",
        }
        url = f"{self.LIST_API}?{urlencode(params)}"
        payload = self._curl_json(
            url,
            context=f"list page {page}",
            accept="application/json,*/*;q=0.8",
        )
        if payload is None:
            return None
        if not isinstance(payload, list):
            print(f"[{self.site_id}] unexpected list payload on page {page}")
            return None
        return payload

    def _fetch_detail(self, article_id):
        url = self.DETAIL_API.format(article_id=article_id)
        payload = self._curl_json(
            url,
            context=f"detail {article_id}",
            accept="application/json,*/*;q=0.8",
        )
        if not isinstance(payload, dict):
            return None
        return payload

    def _curl_json(self, url, context="request", accept=None):
        raw = self._curl_get_text(url, context=context, accept=accept)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] {context} JSON parse failed: {exc}")
            return None

    def _curl_get_text(self, url, context="request", accept=None):
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
            f"Accept: {accept or 'application/json,text/html,*/*;q=0.8'}",
            "-H",
            "Accept-Language: en-US,en;q=0.9",
            "-w",
            write_out,
            url,
        ]

        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.CURL_TIMEOUT + 10,
                )
                stdout = (result.stdout or b"").decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                body, meta = self._split_curl_output(stdout, url)
                http_code = self._safe_int(meta.get("http_code"))

                if result.returncode != 0:
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if http_code is not None and http_code >= 400:
                    raise RuntimeError(f"HTTP {http_code} for {meta.get('effective_url') or url}")
                if not body.strip():
                    raise RuntimeError(f"empty response for {meta.get('effective_url') or url}")
                return body
            except Exception as exc:
                last_error = str(exc)
                print(
                    f"[{self.site_id}] {context} curl failed "
                    f"(attempt {attempt}/3): {last_error}"
                )
                if attempt < 3:
                    wait = self.BACKOFF_SECONDS[attempt - 1]
                    print(f"[{self.site_id}] retrying in {wait}s...")
                    time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None

    def _split_curl_output(self, raw, fallback_url):
        marker_pos = raw.rfind("\n" + self._CURL_META_MARKER)
        if marker_pos == -1:
            return raw, {"effective_url": fallback_url}
        body = raw[:marker_pos]
        meta_raw = raw[marker_pos + 1 + len(self._CURL_META_MARKER):].strip()
        parts = meta_raw.split("\t")
        while len(parts) < 2:
            parts.append("")
        return body, {
            "http_code": parts[0].strip(),
            "effective_url": parts[1].strip() or fallback_url,
        }

    # ------------------------------------------------------------------
    # Record mapping
    # ------------------------------------------------------------------

    def _build_paper(self, list_item, detail):
        article_id = str(detail.get("id") or list_item.get("id") or "").strip()
        custom_fields = self._custom_fields_map(detail.get("custom_fields") or [])
        custom_fields_raw = self._compact_custom_fields(detail.get("custom_fields") or [])
        timeline = detail.get("timeline") or list_item.get("timeline") or {}

        title = self._html_to_text(detail.get("title") or list_item.get("title") or "")
        abstract = self._html_to_text(detail.get("description") or "")

        listed_raw = (
            timeline.get("posted")
            or detail.get("published_date")
            or list_item.get("published_date")
            or detail.get("created_date")
            or list_item.get("created_date")
            or ""
        )
        listed_date = self._date_only(listed_raw)

        publication_date_raw = self._first_value(custom_fields.get("Publication Date"))
        published_raw = (
            publication_date_raw
            or timeline.get("publisherPublication")
            or timeline.get("firstOnline")
            or detail.get("published_date")
            or list_item.get("published_date")
            or ""
        )
        published_date = self._date_only(published_raw) or listed_date

        authors = self._join(
            [
                self._clean_text(author.get("full_name") or "")
                for author in detail.get("authors") or []
                if isinstance(author, dict)
            ],
            sep="; ",
        )
        departments = self._as_list(custom_fields.get("Department/Unit"))
        department = self._join(departments, sep="; ")
        publisher = (
            self._join(self._as_list(custom_fields.get("Publisher")), sep="; ")
            or "RCSI University of Medicine and Health Sciences"
        )

        keywords = self._join(
            detail.get("keywords") or detail.get("tags") or [],
            sep=", ",
        )
        categories = [
            c.get("title")
            for c in detail.get("categories") or []
            if isinstance(c, dict) and c.get("title")
        ]
        category = detail.get("defined_type_name") or list_item.get("defined_type_name") or None

        journal_raw = (
            detail.get("resource_title")
            or self._first_value(custom_fields.get("Journal"))
            or self._first_value(custom_fields.get("Publication Title"))
        )
        series = self._first_value(custom_fields.get("Series"))
        volume = self._first_value(custom_fields.get("Volume"))
        issue = self._first_value(custom_fields.get("Issue"))
        citation = self._first_value(custom_fields.get("Published Citation"))
        if (not volume or not issue) and citation:
            parsed_volume, parsed_issue = self._volume_issue_from_citation(citation)
            volume = volume or parsed_volume
            issue = issue or parsed_issue

        external_doi = self._first_value(custom_fields.get("External DOI"))
        doi = self._normalize_doi(external_doi or detail.get("resource_doi") or detail.get("doi"))

        selected_file = self._select_pdf_file(detail.get("files") or [])
        pdf_url = selected_file.get("download_url") if selected_file else None
        original_filename = None
        if selected_file:
            original_filename = (
                selected_file.get("name")
                or self._filename_from_url(selected_file.get("download_url"))
            )

        detail_url = (
            detail.get("url_public_html")
            or list_item.get("url_public_html")
            or self._record_url(list_item)
            or f"{self.base_url}/articles/{article_id}"
        )

        metadata = {
            "posted_date": listed_raw,
            "listed_date": listed_date,
            "originalFilename": original_filename,
            "journal_raw": journal_raw,
            "series": series,
            "volume": volume,
            "issue": issue,
            "article_id": article_id,
            "figshare_id": article_id,
            "node_id": article_id,
            "post_number": article_id,
            "group_id": detail.get("group_id") or list_item.get("group_id"),
            "defined_type": detail.get("defined_type") or list_item.get("defined_type"),
            "defined_type_name": category,
            "handle": detail.get("handle") or list_item.get("handle"),
            "version": detail.get("version"),
            "status": detail.get("status"),
            "created_date": detail.get("created_date") or list_item.get("created_date"),
            "modified_date": detail.get("modified_date") or list_item.get("modified_date"),
            "published_date_api": detail.get("published_date") or list_item.get("published_date"),
            "published_date_raw": published_raw,
            "custom_fields": custom_fields,
            "custom_fields_raw": custom_fields_raw,
            "categories": categories,
            "category_records": self._compact_categories(detail.get("categories") or []),
            "files": self._compact_files(detail.get("files") or []),
            "selected_file_id": selected_file.get("id") if selected_file else None,
            "timeline": timeline,
            "license": detail.get("license"),
            "funding": detail.get("funding"),
            "funding_list": detail.get("funding_list"),
            "references": detail.get("references"),
            "related_materials": detail.get("related_materials"),
            "resource_title": detail.get("resource_title"),
            "resource_doi": detail.get("resource_doi"),
            "citation": detail.get("citation"),
            "figshare_api_url": detail.get("url") or list_item.get("url"),
            "url_public_api": detail.get("url_public_api") or list_item.get("url_public_api"),
            "url_public_html": detail_url,
            "download_disabled": detail.get("download_disabled"),
            "is_metadata_record": detail.get("is_metadata_record"),
            "is_embargoed": detail.get("is_embargoed"),
            "embargo_date": detail.get("embargo_date"),
            "list_record": self._compact_list_record(list_item),
        }

        return {
            "id": f"{self.site_id}:{article_id}",
            "site_id": self.site_id,
            "external_id": article_id,
            "post_number": article_id,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": authors,
            "publisher": publisher,
            "department": department,
            "journal": journal_raw,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _record_url(self, item):
        if not isinstance(item, dict):
            return ""
        return (
            item.get("url_public_html")
            or item.get("figshare_url")
            or item.get("url")
            or ""
        )

    def _custom_fields_map(self, fields):
        result = {}
        for field in fields:
            if not isinstance(field, dict):
                continue
            name = self._clean_text(field.get("name") or "")
            if not name:
                continue
            result[name] = field.get("value")
        return result

    def _compact_custom_fields(self, fields):
        compact = []
        for field in fields:
            if not isinstance(field, dict):
                continue
            compact.append(
                {
                    "name": field.get("name"),
                    "value": field.get("value"),
                    "field_type": field.get("field_type"),
                    "is_mandatory": field.get("is_mandatory"),
                    "order": field.get("order"),
                }
            )
        return compact

    def _compact_categories(self, categories):
        compact = []
        for category in categories:
            if not isinstance(category, dict):
                continue
            compact.append(
                {
                    "id": category.get("id"),
                    "title": category.get("title"),
                    "parent_id": category.get("parent_id"),
                    "path": category.get("path"),
                    "source_id": category.get("source_id"),
                    "taxonomy_id": category.get("taxonomy_id"),
                }
            )
        return compact

    def _compact_files(self, files):
        compact = []
        for item in files:
            if not isinstance(item, dict):
                continue
            compact.append(
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "size": item.get("size"),
                    "mimetype": item.get("mimetype"),
                    "download_url": item.get("download_url"),
                    "is_link_only": item.get("is_link_only"),
                    "supplied_md5": item.get("supplied_md5"),
                    "computed_md5": item.get("computed_md5"),
                }
            )
        return compact

    def _compact_list_record(self, item):
        if not isinstance(item, dict):
            return {}
        return {
            "id": item.get("id"),
            "title": self._html_to_text(item.get("title") or ""),
            "doi": item.get("doi"),
            "handle": item.get("handle"),
            "url": item.get("url"),
            "published_date": item.get("published_date"),
            "defined_type": item.get("defined_type"),
            "defined_type_name": item.get("defined_type_name"),
            "group_id": item.get("group_id"),
            "url_public_api": item.get("url_public_api"),
            "url_public_html": item.get("url_public_html"),
            "timeline": item.get("timeline"),
            "created_date": item.get("created_date"),
            "modified_date": item.get("modified_date"),
            "resource_title": item.get("resource_title"),
            "resource_doi": item.get("resource_doi"),
        }

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, raw, context="HTML"):
        try:
            from bs4 import BeautifulSoup
        except Exception as exc:
            print(f"[{self.site_id}] BeautifulSoup unavailable for {context}: {exc}")
            return None

        if isinstance(raw, bytes):
            text = raw.decode("utf-8", errors="replace")
        else:
            text = str(raw or "")

        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                last_exc = exc
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        print(f"[{self.site_id}] all BeautifulSoup parsers failed for {context}: {last_exc}")
        return None

    def _html_to_text(self, value):
        if value is None:
            return ""
        text = str(value)
        if not text:
            return ""
        if "<" not in text and "&" not in text:
            return self._clean_text(text)

        soup = self._make_soup(text, context="HTML fragment")
        if soup is not None:
            try:
                for tag in soup.select("script, style, noscript"):
                    tag.decompose()
                return self._clean_text(soup.get_text(" ", strip=True))
            except Exception:
                pass

        text = re.sub(r"<[^>]+>", " ", text)
        return self._clean_text(text)

    @staticmethod
    def _clean_text(value):
        text = unescape(str(value or ""))
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _date_only(self, raw):
        value = self._first_value(raw)
        if not value:
            return None
        text = self._clean_text(value)
        if not text:
            return None

        match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
        if match:
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"

        match = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", text)
        if match:
            day, month, year = match.groups()
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"

        match = re.search(
            r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})\b",
            text,
            re.IGNORECASE,
        )
        if match:
            day, month_name, year = match.groups()
            month = self.MONTHS.get(month_name.lower())
            if month:
                return f"{int(year):04d}-{month:02d}-{int(day):02d}"

        match = re.search(
            r"\b([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b",
            text,
            re.IGNORECASE,
        )
        if match:
            month_name, day, year = match.groups()
            month = self.MONTHS.get(month_name.lower())
            if month:
                return f"{int(year):04d}-{month:02d}-{int(day):02d}"

        match = re.fullmatch(r"\d{4}", text)
        if match:
            return text

        return None

    def _select_pdf_file(self, files):
        pdfs = []
        for item in files:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            mimetype = str(item.get("mimetype") or "")
            download_url = str(item.get("download_url") or "")
            if (
                mimetype.lower() == "application/pdf"
                or name.lower().endswith(".pdf")
                or self._filename_from_url(download_url).lower().endswith(".pdf")
            ):
                pdfs.append(item)
        return pdfs[0] if pdfs else None

    @staticmethod
    def _filename_from_url(url):
        if not url:
            return ""
        path = urlparse(url).path
        filename = unquote(path.rstrip("/").rsplit("/", 1)[-1])
        return filename or ""

    def _normalize_doi(self, value):
        raw = self._first_value(value)
        if not raw:
            return None
        text = self._clean_text(raw)
        if not text:
            return None
        if text.lower().startswith("http://dx.doi.org/"):
            return "https://doi.org/" + text.split("/", 3)[-1]
        if text.lower().startswith("https://doi.org/"):
            return text
        if text.startswith("10."):
            return "https://doi.org/" + text
        return text

    def _volume_issue_from_citation(self, citation):
        text = self._clean_text(citation)
        match = re.search(r"\b(\d+)\s*\(([^)]+)\)", text)
        if match:
            return match.group(1), match.group(2)
        match = re.search(r"\b(\d+)\s*:\s*(\d+)\b", text)
        if match:
            return match.group(1), match.group(2)
        return None, None

    def _as_list(self, value):
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return [self._clean_text(v) for v in value if self._clean_text(v)]
        return [self._clean_text(value)] if self._clean_text(value) else []

    def _first_value(self, value):
        if isinstance(value, list):
            for item in value:
                text = self._clean_text(item)
                if text:
                    return text
            return None
        text = self._clean_text(value)
        return text or None

    def _join(self, values, sep):
        seen = set()
        result = []
        for value in values or []:
            text = self._clean_text(value)
            key = text.lower()
            if not text or key in seen:
                continue
            seen.add(key)
            result.append(text)
        return sep.join(result) if result else None

    def _budget_nearly_exhausted(self, started_at):
        return (time.time() - started_at) >= (self.WALL_CLOCK_SECONDS - 30)

    @staticmethod
    def _safe_int(value):
        try:
            if value in (None, ""):
                return None
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None
