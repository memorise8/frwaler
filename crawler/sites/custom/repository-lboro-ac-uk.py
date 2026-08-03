# -*- coding: utf-8 -*-
"""Crawler for Loughborough University Research Repository.

Starting URL:
    https://repository.lboro.ac.uk/

Discovery notes:
    The repository front door returns an AWS WAF challenge to plain curl, but
    public records are available through Figshare's API. A real repository item
    at /articles/online_resource/.../28882631 maps to:

      Detail GET https://api.figshare.com/v2/articles/28882631

    The detail payload identifies group_id=2, and the list endpoint is:

      List   GET https://api.figshare.com/v2/articles?group=2&limit=...&offset=...
      Detail GET https://api.figshare.com/v2/articles/{article_id}
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from html import unescape
from urllib.parse import unquote, urlencode, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class RepositoryLboroAcUkCrawler(BaseCrawler):
    site_id = "repository-lboro-ac-uk"
    site_name = "Custom: repository-lboro-ac-uk"
    base_url = "https://repository.lboro.ac.uk"

    API_BASE = "https://api.figshare.com/v2"
    LIST_API = API_BASE + "/articles"
    DETAIL_API = API_BASE + "/articles/{article_id}"
    GROUP_ID = "2"

    PAGE_SIZE = 100
    SAFETY_PAGE_CAP = 200
    WALL_CLOCK_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    CURL_TIMEOUT = 45
    BACKOFF_SECONDS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 50
    _CURL_META_MARKER = "__REPOSITORY_LBORO_AC_UK_CURL_META__:"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    def crawl(self, limit=None):
        """Crawl public Figshare-backed repository records."""
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
                        print(f"[{self.site_id}] item {item_label} skipped: missing article id")
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
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.CURL_TIMEOUT + 10,
                    check=False,
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                body = result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                payload, http_code, effective_url = self._split_curl_meta(body)
                if result.returncode == 0 and payload and 200 <= http_code < 400:
                    return payload
                last_error = (
                    f"exit={result.returncode} http={http_code} "
                    f"url={effective_url or url} {stderr[:200]}"
                )

            if attempt < 2:
                wait = self.BACKOFF_SECONDS[attempt]
                print(
                    f"[{self.site_id}] {context} curl attempt {attempt + 1}/3 "
                    f"failed: {last_error}; retry in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None

    def _split_curl_meta(self, text):
        marker_pos = text.rfind(self._CURL_META_MARKER)
        if marker_pos < 0:
            return text, 0, ""
        payload = text[:marker_pos].rstrip("\n")
        meta = text[marker_pos + len(self._CURL_META_MARKER) :].strip()
        parts = meta.split("\t", 1)
        try:
            http_code = int(parts[0])
        except (TypeError, ValueError):
            http_code = 0
        effective_url = parts[1] if len(parts) > 1 else ""
        return payload, http_code, effective_url

    # ------------------------------------------------------------------
    # Mapping
    # ------------------------------------------------------------------

    def _build_paper(self, list_item, detail):
        article_id = str(detail.get("id") or list_item.get("id") or "").strip()
        if not article_id:
            raise ValueError("missing article id")

        title = self._clean(detail.get("title") or list_item.get("title") or "")
        if not title:
            raise ValueError("missing title")

        detail_url = (
            detail.get("figshare_url")
            or detail.get("url_public_html")
            or list_item.get("url_public_html")
            or f"{self.base_url}/articles/{article_id}"
        )

        timeline = self._as_dict(detail.get("timeline") or list_item.get("timeline"))
        listed_raw = timeline.get("posted") or detail.get("published_date") or list_item.get("published_date")
        listed_date = self._iso_date(listed_raw)
        published_raw = (
            timeline.get("publisherPublication")
            or timeline.get("firstOnline")
            or detail.get("published_date")
            or list_item.get("published_date")
        )
        published_date = self._iso_date(published_raw)

        custom_fields = self._custom_fields_dict(detail.get("custom_fields"))
        school = self._first_custom(custom_fields, "school")
        department = self._first_custom(custom_fields, "department")
        research_unit = self._first_custom(custom_fields, "research unit")

        journal_raw = (
            self._first_custom(
                custom_fields,
                "journal",
                "journal title",
                "publication",
                "published in",
                "publication title",
                "source title",
            )
            or ""
        )
        journal = journal_raw or None
        if not journal and self._clean(detail.get("defined_type_name")).lower() == "journal contribution":
            journal = self._clean(detail.get("resource_title") or "") or None

        series = self._first_custom(custom_fields, "series")
        volume = self._first_custom(custom_fields, "volume")
        issue = self._first_custom(custom_fields, "issue", "number")

        pdf_file = self._select_pdf_file(detail.get("files"))
        pdf_url = pdf_file.get("download_url") if pdf_file else None
        original_filename = self._original_filename(pdf_file, pdf_url)

        authors = self._authors(detail.get("authors"))
        keywords = self._keywords(detail.get("keywords") or detail.get("tags"))
        category = self._category(detail)
        abstract = self._html_to_text(detail.get("description") or "")
        publisher = "Loughborough University"
        doi = self._clean(detail.get("doi") or list_item.get("doi") or "")

        metadata = {
            "article_id": article_id,
            "node_id": article_id,
            "post_number": article_id,
            "group_id": detail.get("group_id") or list_item.get("group_id"),
            "posted_date": listed_raw,
            "listed_date": listed_date,
            "published_date_raw": published_raw,
            "originalFilename": original_filename,
            "journal_raw": journal_raw or detail.get("resource_title"),
            "series": series,
            "volume": volume,
            "issue": issue,
            "figshare_url": detail.get("figshare_url"),
            "url_public_html": detail.get("url_public_html") or list_item.get("url_public_html"),
            "url_public_api": detail.get("url_public_api") or list_item.get("url_public_api"),
            "defined_type": detail.get("defined_type"),
            "defined_type_name": detail.get("defined_type_name"),
            "resource_title": detail.get("resource_title"),
            "resource_doi": detail.get("resource_doi"),
            "handle": detail.get("handle"),
            "version": detail.get("version"),
            "status": detail.get("status"),
            "license": detail.get("license"),
            "school": school,
            "department": department,
            "research_unit": research_unit,
            "custom_fields": detail.get("custom_fields"),
            "files": detail.get("files"),
            "categories": detail.get("categories"),
            "references": detail.get("references"),
            "related_materials": detail.get("related_materials"),
            "funding": detail.get("funding"),
            "funding_list": detail.get("funding_list"),
            "timeline": timeline,
            "raw_list_item": list_item,
            "raw_detail": detail,
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
            "department": department or school,
            "journal": journal,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": doi or None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False, default=str),
        }

    def _record_url(self, item):
        return (
            item.get("url_public_html")
            or item.get("figshare_url")
            or item.get("url")
            or item.get("url_public_api")
        )

    @staticmethod
    def _as_dict(value):
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _clean(text):
        if text is None:
            return ""
        text = unescape(str(text))
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _html_to_text(self, raw):
        if not raw:
            return ""
        soup = self._parse_html(str(raw))
        if soup is not None:
            text = soup.get_text(" ", strip=True)
        else:
            text = re.sub(r"<[^>]+>", " ", str(raw))
        return self._clean(text)

    @staticmethod
    def _parse_html(raw):
        if not raw:
            return None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception:
                continue
        return None

    def _custom_fields_dict(self, fields):
        result = {}
        if not isinstance(fields, list):
            return result
        for field in fields:
            if not isinstance(field, dict):
                continue
            name = self._clean(field.get("name")).lower()
            if not name:
                continue
            result[name] = field.get("value")
        return result

    def _first_custom(self, custom_fields, *names):
        for name in names:
            value = custom_fields.get(name.lower())
            cleaned = self._stringify_list_value(value)
            if cleaned:
                return cleaned
        return None

    def _stringify_list_value(self, value):
        if value is None or value == "":
            return None
        if isinstance(value, list):
            parts = [self._clean(v) for v in value if self._clean(v)]
            return "; ".join(parts) if parts else None
        cleaned = self._clean(value)
        return cleaned or None

    def _authors(self, authors):
        if not isinstance(authors, list):
            return None
        names = []
        for author in authors:
            if not isinstance(author, dict):
                continue
            name = self._clean(author.get("full_name"))
            if name:
                names.append(name)
        return "; ".join(names) if names else None

    def _keywords(self, keywords):
        if not isinstance(keywords, list):
            return None
        vals = [self._clean(k) for k in keywords if self._clean(k)]
        return ", ".join(vals) if vals else None

    def _category(self, detail):
        type_name = self._clean(detail.get("defined_type_name"))
        categories = []
        for category in detail.get("categories") or []:
            if isinstance(category, dict):
                title = self._clean(category.get("title") or category.get("path"))
                if title:
                    categories.append(title)
            else:
                title = self._clean(category)
                if title:
                    categories.append(title)
        parts = [p for p in [type_name, "; ".join(categories)] if p]
        return " | ".join(parts) if parts else None

    def _select_pdf_file(self, files):
        if not isinstance(files, list):
            return None
        for file_info in files:
            if not isinstance(file_info, dict):
                continue
            name = self._clean(file_info.get("name"))
            mimetype = self._clean(file_info.get("mimetype")).lower()
            if name.lower().endswith(".pdf") or "pdf" in mimetype:
                return file_info
        return None

    def _original_filename(self, file_info, pdf_url):
        if isinstance(file_info, dict):
            name = self._clean(file_info.get("name"))
            if name:
                return name
        if not pdf_url:
            return None
        tail = unquote(urlparse(pdf_url).path.rstrip("/").split("/")[-1])
        if tail and "." in tail and len(tail) <= 200:
            return tail
        return None

    @staticmethod
    def _iso_date(value):
        if not value:
            return None
        text = str(value).strip()
        match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
        if match:
            return match.group(1)
        match = re.search(r"\b(\d{4})\b", text)
        if match:
            return match.group(1)
        return None

    def _budget_nearly_exhausted(self, started_at):
        return (time.time() - started_at) >= (self.WALL_CLOCK_SECONDS - 30)
