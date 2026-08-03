# -*- coding: utf-8 -*-
"""Crawler for ISSI Bern outreach news.

List source:
    https://www.issibern.ch/outreach/news/

The page is rendered by WordPress + FacetWP. The usable list endpoint is the
FacetWP refresh REST endpoint exposed in ``window.FWP_JSON``. It returns each
news page as HTML fragments; detail pages carry the native WordPress post ID
and full article metadata.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

# spec_from_file_location gives this module no package context, so keep the
# repository root importable and use an absolute import.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402


class IssibernChOutreachCrawler(BaseCrawler):
    site_id = "issibern-ch-outreach"
    site_name = "Custom: issibern-ch-outreach"
    base_url = "https://www.issibern.ch"

    START_URL = "https://www.issibern.ch/outreach/news/"
    DEFAULT_LIST_ENDPOINT = "https://www.issibern.ch/wp-json/facetwp/v1/refresh"
    SAFETY_PAGE_CAP = 200
    WALL_CLOCK_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    CURL_TIMEOUT = 45
    BACKOFF_SECONDS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 50
    _CURL_META_MARKER = "__ISSIBERN_CH_OUTREACH_CURL_META__:"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    def crawl(self, limit=None):
        """Crawl ISSI Bern outreach news records."""
        saved = 0
        page = 1
        seen_urls = set()
        started_at = time.time()
        limit_label = str(limit) if limit is not None else "inf"

        config = self._discover_list_config()
        print(f"[{self.site_id}] list endpoint: {config['ajaxurl']}")

        while page <= self.SAFETY_PAGE_CAP:
            if limit is not None and saved >= limit:
                break
            if self._budget_nearly_exhausted(started_at):
                print(f"[{self.site_id}] time budget nearly exhausted; stopping cleanly")
                break
            if page == 1 or page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            page_data = self._fetch_list_page(config, page)
            if page_data is None:
                print(f"[{self.site_id}] list page {page} failed; stopping")
                break

            records = page_data.get("records") or []
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
                    detail_url = self._normalize_url(record.get("url"))
                    if not detail_url:
                        print(f"[{self.site_id}] item {item_label} skipped: missing URL")
                        continue
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_on_page += 1

                    time.sleep(self.detail_delay)
                    raw_detail, _, _ = self._curl_get_text(
                        detail_url,
                        context=f"detail {detail_url}",
                        referer=self.START_URL,
                    )
                    if not raw_detail:
                        raise RuntimeError("detail fetch failed after retries")

                    detail = self._parse_detail(raw_detail, detail_url, record)
                    paper = self._to_paper(record, detail, page, idx, config, page_data)

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
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page} had no unseen URLs; stopping")
                break

            pager = page_data.get("pager") or {}
            total_pages = self._safe_int(pager.get("total_pages"))
            current_page = self._safe_int(pager.get("page"), page) or page
            if total_pages is not None and current_page >= total_pages:
                break
            page += 1

        if page > self.SAFETY_PAGE_CAP:
            print(f"[{self.site_id}] reached safety page cap ({self.SAFETY_PAGE_CAP}); stopping")

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # List discovery and fetching
    # ------------------------------------------------------------------

    def _discover_list_config(self):
        raw, _, _ = self._curl_get_text(
            self.START_URL,
            context="start page",
            referer=self.base_url,
        )
        if not raw:
            raise RuntimeError("start page fetch failed")

        soup = self._make_soup(raw)
        template_name = "news"
        template = soup.select_one(".facetwp-template") if soup else None
        if template and template.get("data-name"):
            template_name = template.get("data-name")

        fwp_json = self._extract_window_json(raw, "FWP_JSON") or {}
        fwp_http = self._extract_window_json(raw, "FWP_HTTP") or {}
        ajaxurl = fwp_json.get("ajaxurl") or self.DEFAULT_LIST_ENDPOINT
        uri = fwp_http.get("uri") or "outreach/news"

        preload = fwp_json.get("preload_data") if isinstance(fwp_json, dict) else {}
        initial_template = ""
        if template:
            initial_template = str(template)

        return {
            "ajaxurl": ajaxurl,
            "template": template_name,
            "uri": uri,
            "nonce": fwp_json.get("nonce"),
            "initial_template": initial_template,
            "initial_raw": raw,
            "preload_pager": ((preload or {}).get("settings") or {}).get("pager") or {},
        }

    def _fetch_list_page(self, config, page):
        payload = {
            "action": "facetwp_refresh",
            "data": {
                "facets": {"search": "", "n_categories": []},
                "frozen_facets": {},
                "http_params": {"get": {}, "uri": config.get("uri") or "outreach/news", "url_vars": []},
                "template": config.get("template") or "news",
                "extras": {},
                "soft_refresh": 1,
                "is_bfcache": 1,
                "first_load": 0,
                "paged": page,
            },
        }
        raw, _, _ = self._curl_get_text(
            config.get("ajaxurl") or self.DEFAULT_LIST_ENDPOINT,
            method="POST",
            json_payload=payload,
            context=f"FacetWP list page {page}",
            referer=self.START_URL,
            accept="application/json,*/*;q=0.8",
        )

        if raw:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"[{self.site_id}] invalid FacetWP JSON on page {page}: {exc}")
                data = None
            if isinstance(data, dict):
                template = data.get("template") or ""
                records = self._parse_list_template(template, page)
                return {
                    "records": records,
                    "pager": ((data.get("settings") or {}).get("pager") or {}),
                    "raw_api": self._compact_api_metadata(data),
                }

        if page == 1 and config.get("initial_template"):
            records = self._parse_list_template(config["initial_template"], page)
            return {
                "records": records,
                "pager": config.get("preload_pager") or {},
                "raw_api": {"fallback": "initial_html_preload"},
            }
        return None

    def _parse_list_template(self, html, page):
        soup = self._make_soup(html)
        if soup is None:
            return []

        records = []
        for idx, item in enumerate(soup.select(".news-item"), start=1):
            title_link = item.select_one("h3 a[href]") or item.select_one(".news-button a[href]")
            title = self._clean_text(title_link.get_text(" ", strip=True)) if title_link else ""
            url = self._normalize_url(title_link.get("href")) if title_link else None
            if not title or not url:
                continue

            date_raw = self._clean_text(
                item.select_one(".news-date").get_text(" ", strip=True)
                if item.select_one(".news-date")
                else ""
            )
            paragraphs = [
                self._clean_text(p.get_text(" ", strip=True))
                for p in item.find_all("p")
                if self._clean_text(p.get_text(" ", strip=True))
            ]
            categories = self._unique_texts(item.select(".news-categories-item"))
            tags = self._unique_texts(item.select(".news-tags-item"))
            slug = self._slug_from_url(url)

            records.append(
                {
                    "title": title,
                    "url": url,
                    "slug": slug,
                    "listed_date_raw": date_raw,
                    "listed_date": self._parse_date(date_raw),
                    "abstract": self._clean_text(" ".join(paragraphs)),
                    "categories": categories,
                    "tags": tags,
                    "list_page": page,
                    "list_index": idx,
                }
            )
        return records

    # ------------------------------------------------------------------
    # Detail parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, raw, url, record):
        soup = self._make_soup(raw)
        if soup is None:
            raise RuntimeError("detail HTML parse failed")

        schema = self._extract_schema(soup)
        article_schema = self._first_schema_type(schema, "Article")
        web_schema = self._first_schema_type(schema, "WebPage")

        post_id = self._extract_post_id(raw, soup)
        title = self._first_nonempty(
            self._clean_text(soup.select_one("h1").get_text(" ", strip=True) if soup.select_one("h1") else ""),
            self._meta_content(soup, "og:title", "property"),
            record.get("title"),
        )

        lead_text = soup.select_one("#news-lead-text-inner")
        paragraphs = []
        if lead_text:
            paragraphs = [
                self._clean_text(p.get_text(" ", strip=True))
                for p in lead_text.find_all("p")
                if self._clean_text(p.get_text(" ", strip=True))
            ]
        if not paragraphs:
            content = soup.select_one("main#content") or soup.select_one("#content")
            if content:
                paragraphs = [
                    self._clean_text(p.get_text(" ", strip=True))
                    for p in content.find_all("p")
                    if self._clean_text(p.get_text(" ", strip=True))
                ]

        abstract = self._clean_text(" ".join(paragraphs)) or record.get("abstract") or ""

        detail_date_raw = self._clean_text(
            soup.select_one("#news-lead-meta-date").get_text(" ", strip=True)
            if soup.select_one("#news-lead-meta-date")
            else ""
        )
        published_raw = self._first_nonempty(
            detail_date_raw,
            self._meta_content(soup, "article:published_time", "property"),
            (article_schema or {}).get("datePublished"),
            (web_schema or {}).get("datePublished"),
        )
        published_date = self._parse_date(published_raw) or record.get("listed_date")

        categories = self._unique_texts(soup.select(".news-lead-meta-category-item")) or record.get("categories") or []
        tags = self._unique_texts(soup.select(".news-lead-meta-tag-item")) or record.get("tags") or []
        info_text = self._clean_text(
            soup.select_one("#news-lead-meta-infotext").get_text(" ", strip=True)
            if soup.select_one("#news-lead-meta-infotext")
            else ""
        )
        meta_author = self._first_nonempty(
            self._meta_content(soup, "author", "name"),
            self._schema_author_name(article_schema),
        )
        authors = self._extract_authors(info_text) or meta_author

        pdf_url = self._find_pdf_url(soup)
        original_filename = self._original_filename(pdf_url)
        doi = self._extract_doi(raw)

        return {
            "post_id": post_id,
            "slug": self._slug_from_url(url),
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "published_raw": published_raw,
            "modified_raw": self._first_nonempty(
                self._meta_content(soup, "article:modified_time", "property"),
                (article_schema or {}).get("dateModified"),
                (web_schema or {}).get("dateModified"),
            ),
            "categories": categories,
            "tags": tags,
            "info_text": info_text,
            "authors": authors,
            "meta_author": meta_author,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "doi": doi,
            "canonical": self._canonical_url(soup) or url,
            "og_description": self._meta_content(soup, "og:description", "property"),
            "schema": schema,
            "article_schema": article_schema,
            "web_schema": web_schema,
        }

    def _to_paper(self, record, detail, page, idx, config, page_data):
        post_number = detail.get("post_id") or record.get("slug") or detail.get("slug")
        external_id = str(post_number) if post_number is not None else detail.get("canonical")
        url = detail.get("canonical") or record.get("url")
        listed_date = record.get("listed_date")
        listed_raw = record.get("listed_date_raw")
        categories = detail.get("categories") or record.get("categories") or []
        tags = detail.get("tags") or record.get("tags") or []
        original_filename = detail.get("original_filename")

        metadata = {
            "posted_date": listed_raw,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "post_id": detail.get("post_id"),
            "wp_post_id": detail.get("post_id"),
            "slug": detail.get("slug") or record.get("slug"),
            "node_id": detail.get("post_id"),
            "listed_date": listed_date,
            "list_page": page,
            "list_index": idx,
            "list_categories": record.get("categories") or [],
            "list_tags": record.get("tags") or [],
            "detail_categories": categories,
            "detail_tags": tags,
            "detail_published_raw": detail.get("published_raw"),
            "detail_modified_raw": detail.get("modified_raw"),
            "detail_info_text": detail.get("info_text"),
            "detail_og_description": detail.get("og_description"),
            "meta_author": detail.get("meta_author"),
            "facetwp_ajaxurl": config.get("ajaxurl"),
            "facetwp_template": config.get("template"),
            "facetwp_nonce": config.get("nonce"),
            "facetwp_pager": page_data.get("pager") or {},
            "facetwp_raw_api": page_data.get("raw_api") or {},
            "schema": detail.get("schema"),
        }

        return {
            "id": f"{self.site_id}:{external_id}",
            "site_id": self.site_id,
            "external_id": str(external_id) if external_id is not None else None,
            "post_number": str(post_number) if post_number is not None else None,
            "title": detail.get("title") or record.get("title"),
            "abstract": detail.get("abstract") or record.get("abstract"),
            "published_date": detail.get("published_date") or listed_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": detail.get("authors"),
            "publisher": "International Space Science Institute",
            "department": "Outreach",
            "journal": None,
            "url": url,
            "pdf_url": detail.get("pdf_url"),
            "keywords": ", ".join(tags) if tags else None,
            "category": "; ".join(categories) if categories else None,
            "doi": detail.get("doi"),
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False, default=str),
        }

    # ------------------------------------------------------------------
    # HTTP and utility helpers
    # ------------------------------------------------------------------

    def _curl_get_text(
        self,
        url,
        *,
        method="GET",
        json_payload=None,
        context="request",
        referer=None,
        accept=None,
        timeout=None,
        retries=3,
    ):
        timeout = timeout or self.CURL_TIMEOUT
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
            str(timeout),
            "-A",
            self.USER_AGENT,
            "-H",
            f"Accept: {accept or 'text/html,application/xhtml+xml,application/json,*/*;q=0.8'}",
            "-H",
            "Accept-Language: en-US,en;q=0.9",
            "-w",
            write_out,
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        input_bytes = None
        if method.upper() == "POST":
            cmd.extend(["-X", "POST", "-H", "Content-Type: application/json", "--data-binary", "@-"])
            input_bytes = json.dumps(json_payload or {}).encode("utf-8")
        cmd.append(url)

        for attempt in range(retries):
            try:
                result = subprocess.run(
                    cmd,
                    input=input_bytes,
                    capture_output=True,
                    timeout=timeout + 5,
                )
                stdout = result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                body, status, effective_url = self._split_curl_output(stdout, url)
                if result.returncode == 0 and 200 <= status < 300:
                    return body, status, effective_url

                print(
                    f"[{self.site_id}] curl error for {context} "
                    f"(attempt {attempt + 1}/{retries}, rc={result.returncode}, "
                    f"status={status}): {stderr}"
                )
            except subprocess.TimeoutExpired as exc:
                print(f"[{self.site_id}] curl timeout for {context} (attempt {attempt + 1}/{retries}): {exc}")
            except Exception as exc:
                print(f"[{self.site_id}] curl failed for {context} (attempt {attempt + 1}/{retries}): {exc}")

            if attempt < retries - 1:
                time.sleep(self.BACKOFF_SECONDS[min(attempt, len(self.BACKOFF_SECONDS) - 1)])

        return None, None, None

    def _split_curl_output(self, text, fallback_url):
        if self._CURL_META_MARKER not in text:
            return text, 0, fallback_url
        body, meta = text.rsplit(self._CURL_META_MARKER, 1)
        parts = meta.strip().split("\t", 1)
        try:
            status = int(parts[0])
        except (TypeError, ValueError):
            status = 0
        effective_url = parts[1] if len(parts) > 1 and parts[1] else fallback_url
        return body, status, effective_url

    def _curl_head_headers(self, url, timeout=20):
        if not url:
            return ""
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skIL",
            "--max-time",
            str(timeout),
            "-A",
            self.USER_AGENT,
            url,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
        except Exception:
            return ""
        if result.returncode != 0:
            return ""
        return result.stdout.decode("utf-8", errors="replace")

    def _make_soup(self, raw):
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup parser {parser} failed: {exc}")
                continue
        return None

    def _extract_window_json(self, raw, name):
        match = re.search(rf"window\.{re.escape(name)}\s*=\s*(\{{.*?\}});\s*</script>", raw or "", re.S)
        if not match:
            return None
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] failed to parse {name}: {exc}")
            return None

    def _extract_schema(self, soup):
        graphs = []
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.get_text(strip=True)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and isinstance(data.get("@graph"), list):
                graphs.extend(data["@graph"])
            elif isinstance(data, list):
                graphs.extend(data)
            elif isinstance(data, dict):
                graphs.append(data)
        return graphs

    def _first_schema_type(self, schema, type_name):
        for item in schema or []:
            item_type = item.get("@type") if isinstance(item, dict) else None
            if item_type == type_name or (isinstance(item_type, list) and type_name in item_type):
                return item
        return None

    def _schema_author_name(self, article_schema):
        if not isinstance(article_schema, dict):
            return None
        author = article_schema.get("author")
        if isinstance(author, dict):
            return author.get("name")
        if isinstance(author, list):
            names = [x.get("name") for x in author if isinstance(x, dict) and x.get("name")]
            return "; ".join(names) if names else None
        if isinstance(author, str):
            return author
        return None

    def _extract_post_id(self, raw, soup):
        body_classes = soup.body.get("class", []) if soup and soup.body else []
        for cls in body_classes:
            match = re.match(r"postid-(\d+)$", str(cls))
            if match:
                return match.group(1)
        for pattern in (
            r"/wp-json/wp/v2/posts/(\d+)",
            r"[?&]p=(\d+)",
            r"\bpostid-(\d+)\b",
        ):
            match = re.search(pattern, raw or "")
            if match:
                return match.group(1)
        return None

    def _canonical_url(self, soup):
        link = soup.find("link", attrs={"rel": "canonical"})
        if link and link.get("href"):
            return self._normalize_url(link.get("href"))
        meta = self._meta_content(soup, "og:url", "property")
        return self._normalize_url(meta)

    def _meta_content(self, soup, key, attr):
        tag = soup.find("meta", attrs={attr: key})
        if tag and tag.get("content"):
            return self._clean_text(tag.get("content"))
        return None

    def _find_pdf_url(self, soup):
        for link in soup.find_all("a", href=True):
            href = self._normalize_url(link.get("href"))
            if not href:
                continue
            parsed = urlparse(href)
            if parsed.path.lower().endswith(".pdf"):
                return href
        return None

    def _original_filename(self, pdf_url):
        if not pdf_url:
            return None
        headers = self._curl_head_headers(pdf_url)
        match = re.search(
            r"(?im)^content-disposition:.*?filename\*?=(?:UTF-8''|\"?)([^\"\r\n;]+)",
            headers,
        )
        if match:
            filename = unquote(match.group(1).strip().strip('"'))
            if filename:
                return filename
        tail = unquote(urlparse(pdf_url).path.rstrip("/").rsplit("/", 1)[-1])
        return tail or None

    def _extract_doi(self, value):
        if not value:
            return None
        text = unquote(str(value))
        match = re.search(r"(10\.\d{4,9}/[^\s\"'<>]+)", text, re.I)
        if not match:
            return None
        doi = match.group(1).strip().rstrip(").,;]}")
        return doi or None

    def _extract_authors(self, text):
        text = self._clean_text(text)
        if not text:
            return None
        match = re.search(
            r"(?i)\b(?:webinar|talk|panel discussion|online panel discussion)\s+with\s+(.+?)(?:\(| recorded\b| thursday\b|$)",
            text,
        )
        if not match:
            return None
        names = self._clean_text(match.group(1))
        if not names:
            return None
        parts = re.split(r"\s+and\s+|,\s*", names)
        parts = [self._clean_text(p) for p in parts if self._clean_text(p)]
        return "; ".join(parts) if parts else names

    def _parse_date(self, raw):
        if not raw:
            return None
        text = self._clean_text(raw)
        text = re.sub(r"(?i)^published:\s*", "", text).strip()
        match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
        if match:
            return match.group(1)
        for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%d.%m.%Y"):
            try:
                return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return None

    def _compact_api_metadata(self, data):
        settings = data.get("settings") if isinstance(data, dict) else {}
        return {
            "settings": settings,
            "template_length": len(data.get("template") or "") if isinstance(data, dict) else None,
        }

    def _unique_texts(self, nodes):
        values = []
        seen = set()
        for node in nodes:
            text = self._clean_text(node.get_text(" ", strip=True))
            if text and text not in seen:
                seen.add(text)
                values.append(text)
        return values

    def _normalize_url(self, url):
        if not url:
            return None
        return urljoin(self.base_url, unescape(str(url)).strip())

    def _slug_from_url(self, url):
        if not url:
            return None
        path = urlparse(url).path.rstrip("/")
        slug = path.rsplit("/", 1)[-1] if path else None
        return unquote(slug) if slug else None

    def _clean_text(self, value):
        if value is None:
            return ""
        text = unescape(str(value)).replace("\xa0", " ")
        return re.sub(r"\s+", " ", text).strip()

    def _first_nonempty(self, *values):
        for value in values:
            if value not in (None, ""):
                return value
        return None

    def _safe_int(self, value, default=None):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _budget_nearly_exhausted(self, started_at):
        return time.time() - started_at > self.WALL_CLOCK_BUDGET_SECONDS - 30
