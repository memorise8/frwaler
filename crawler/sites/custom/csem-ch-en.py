# -*- coding: utf-8 -*-
"""Crawler for CSEM English press releases."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from html import unescape
from urllib.parse import urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class CSEMChEnCrawler(BaseCrawler):
    site_id = "csem-ch-en"
    site_name = "Custom: csem-ch-en"
    base_url = "https://www.csem.ch"

    START_URL = "https://www.csem.ch/en/press/"
    API_ROOT = "https://csem.cdn.prismic.io/api/v2"
    SEARCH_ENDPOINT = "https://csem.cdn.prismic.io/api/v2/documents/search"
    LANG = "en-gb"
    PAGE_SIZE = 50
    MAX_PAGES = 200
    MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    WALL_MARGIN_SECONDS = 60
    MIN_ABSTRACT_CHARS = 50
    CURL_TIMEOUT = 45
    BACKOFF_SECONDS = (1, 3, 9)
    _CURL_META_MARKER = "__CSEM_CH_EN_CURL_META__:"
    _PDF_RE = re.compile(r"\.pdf(?:[?#].*)?$", re.I)

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    def crawl(self, limit=None):
        """Crawl CSEM's Prismic-backed English press room."""
        if limit is not None and limit <= 0:
            return 0

        start_time = time.monotonic()
        limit_or_inf = limit if limit is not None else "inf"
        saved = 0
        page = 1
        seen_urls = set()
        hit_cap = False

        ref = self._fetch_master_ref()
        if not ref:
            print(f"[{self.site_id}] could not discover Prismic master ref; stopping")
            return 0

        print(
            f"[{self.site_id}] using Prismic endpoint {self.SEARCH_ENDPOINT} "
            f"with pageSize={self.PAGE_SIZE}"
        )

        while page <= self.MAX_PAGES:
            if limit is not None and saved >= limit:
                break
            if self._near_deadline(start_time):
                print(f"[{self.site_id}] approaching 25 minute wall-clock budget; exiting cleanly")
                break

            list_url = self._list_url(ref, page)
            data = self._curl_json(
                list_url,
                context=f"list page {page}",
                accept="Accept: application/json,*/*;q=0.8",
                referer=self.START_URL,
            )
            if not data:
                print(f"[{self.site_id}] page {page}: list endpoint failed; stopping")
                break

            records = data.get("results") or []
            if not records:
                print(f"[{self.site_id}] page {page}: 0 records; stopping")
                break

            new_on_page = 0
            for item_no, record in enumerate(records, start=1):
                if limit is not None and saved >= limit:
                    break
                if self._near_deadline(start_time):
                    print(f"[{self.site_id}] approaching 25 minute wall-clock budget; exiting cleanly")
                    return saved

                item_label = f"page {page} item {item_no}"
                dedupe_url = self._dedupe_url(self._record_url(record) or record.get("id") or "")
                if not dedupe_url:
                    print(f"[{self.site_id}] item {item_label} skipped: missing URL and id")
                    continue
                if dedupe_url in seen_urls:
                    continue
                seen_urls.add(dedupe_url)
                new_on_page += 1

                try:
                    time.sleep(self.detail_delay)
                    detail = self._fetch_detail_record(record, item_label, ref)
                    if not detail:
                        raise RuntimeError("detail fetch failed after retries")

                    parsed = self._parse_record(detail, list_record=record, page=page)
                    abstract = parsed.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        fallback = self._fetch_public_html_text(parsed.get("csem_url"), item_label)
                        if len(fallback) > len(abstract):
                            parsed["abstract"] = fallback
                            abstract = fallback

                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(self._paper_from_parsed(parsed))
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if page % 10 == 0:
                p = page
                print(f"[csem-ch-en] page {p}: saved {saved}/{limit_or_inf}")

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: no new URLs; stopping")
                break
            if not data.get("next_page"):
                break

            page += 1
        else:
            hit_cap = True

        if hit_cap:
            print(f"[{self.site_id}] reached safety cap of {self.MAX_PAGES} pages")
        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _fetch_master_ref(self):
        data = self._curl_json(
            self.API_ROOT,
            context="Prismic API root",
            accept="Accept: application/json,*/*;q=0.8",
            referer=self.START_URL,
        )
        for ref in (data or {}).get("refs", []) or []:
            if ref.get("isMasterRef") or ref.get("id") == "master":
                return ref.get("ref")
        return None

    def _list_url(self, ref, page):
        params = [
            ("ref", ref),
            ("q", '[[at(document.type,"press")]]'),
            ("q", '[[fulltext(document, "")]]'),
            ("orderings", "[my.press.publicationDate desc]"),
            ("page", str(page)),
            ("pageSize", str(self.PAGE_SIZE)),
            ("lang", self.LANG),
        ]
        return f"{self.SEARCH_ENDPOINT}?{urlencode(params)}"

    def _fetch_detail_record(self, record, item_label, ref):
        detail_url = record.get("href") or ""
        if not detail_url:
            doc_id = record.get("id")
            if not doc_id:
                return None
            params = [
                ("ref", ref),
                ("q", f'[[at(document.id,"{doc_id}")]]'),
                ("lang", self.LANG),
            ]
            detail_url = f"{self.SEARCH_ENDPOINT}?{urlencode(params)}"

        data = self._curl_json(
            detail_url,
            context=f"item {item_label} detail API",
            accept="Accept: application/json,*/*;q=0.8",
            referer=self.START_URL,
        )
        results = (data or {}).get("results") or []
        return results[0] if results else None

    def _curl_json(self, url, context="request", accept=None, referer=None):
        raw, _effective_url = self._curl_get(
            url,
            context=context,
            accept=accept,
            referer=referer,
        )
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] {context} returned invalid JSON: {exc}")
            return None

    def _curl_get(self, url, context="request", accept=None, referer=None):
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
            accept
            or (
                "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,"
                "application/json,*/*;q=0.8"
            ),
            "-H",
            "Accept-Language: en-GB,en;q=0.9",
            "-w",
            "\n" + self._CURL_META_MARKER + "%{http_code}\t%{url_effective}",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

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
                body, http_code, effective_url = self._split_curl_output(stdout, url)

                if result.returncode != 0:
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if http_code and int(http_code) >= 400:
                    raise RuntimeError(f"HTTP {http_code} for {effective_url}")
                if not body.strip():
                    raise RuntimeError(f"empty response for {effective_url}")
                return body, effective_url
            except KeyboardInterrupt:
                raise
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
        return None, url

    def _split_curl_output(self, raw, fallback_url):
        marker_pos = raw.rfind("\n" + self._CURL_META_MARKER)
        if marker_pos == -1:
            return raw, "", fallback_url
        body = raw[:marker_pos]
        meta = raw[marker_pos + 1 + len(self._CURL_META_MARKER):].strip()
        if "\t" not in meta:
            return body, "", fallback_url
        http_code, effective_url = meta.split("\t", 1)
        return body, http_code.strip(), effective_url.strip() or fallback_url

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_record(self, record, list_record=None, page=None):
        source_record = list_record or record
        data = record.get("data") or {}
        title = self._rich_text(data.get("title"))
        if not title:
            title = self._clean_text((record.get("slugs") or [""])[0].replace("-", " "))
        if not title:
            raise RuntimeError("missing title")

        content_parts = [
            self._rich_text(data.get("preContent")),
            self._rich_text(data.get("content")),
            self._slice_body_text(data.get("slices") or []),
        ]
        abstract = self._clean_text("\n\n".join(part for part in content_parts if part))

        csem_url = self._csem_url(source_record)
        url = self._record_url(source_record) or csem_url or self.START_URL
        pdf_url = self._pdf_url(data)
        publication_date = self._publication_date(record)
        keywords = self._keywords(data)
        category = "; ".join(self._taxonomy_labels(data, "industries", "industry") or [])
        if not category:
            category = "; ".join(self._taxonomy_labels(data, "domains", "domain") or [])

        metadata = {
            "source": "CSEM Prismic Content API",
            "api_root": self.API_ROOT,
            "list_endpoint": self.SEARCH_ENDPOINT,
            "list_page": page,
            "detail_endpoint": record.get("href"),
            "prismic_id": record.get("id"),
            "uid": record.get("uid"),
            "lang": record.get("lang"),
            "slugs": record.get("slugs") or [],
            "csem_url": csem_url,
            "external_link": self._external_link(data),
            "featured_image": (data.get("featuredImage") or {}).get("url"),
            "publication_date": data.get("publicationDate"),
            "first_publication_date": record.get("first_publication_date"),
            "last_publication_date": record.get("last_publication_date"),
            "domains": self._taxonomy_labels(data, "domains", "domain"),
            "industries": self._taxonomy_labels(data, "industries", "industry"),
            "technical_focuses": self._taxonomy_labels(data, "technicalFocuses", "technicalFocus"),
            "alternate_languages": record.get("alternate_languages") or [],
        }

        if pdf_url:
            metadata["original_filename"] = urlparse(pdf_url).path.rsplit("/", 1)[-1]

        return {
            "external_id": record.get("id") or self._hash_id(url),
            "title": title,
            "abstract": abstract,
            "category": category,
            "keywords": keywords,
            "published_date": publication_date,
            "url": url,
            "csem_url": csem_url,
            "pdf_url": pdf_url,
            "doi": "",
            "department": "CSEM",
            "metadata": metadata,
        }

    def _paper_from_parsed(self, parsed):
        external_id = parsed["external_id"]
        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.site_id}:{external_id}")),
            "site_id": self.site_id,
            "external_id": external_id,
            "title": parsed["title"],
            "authors": json.dumps([], ensure_ascii=False),
            "abstract": parsed["abstract"],
            "category": parsed["category"],
            "keywords": json.dumps(parsed["keywords"], ensure_ascii=False),
            "published_date": parsed["published_date"],
            "url": parsed["url"],
            "pdf_url": parsed["pdf_url"],
            "doi": parsed["doi"],
            "department": parsed["department"],
            "metadata": json.dumps(parsed["metadata"], ensure_ascii=False),
        }

    def _record_url(self, record):
        data = record.get("data") or {}
        external = self._external_link(data)
        if external:
            return external
        return self._csem_url(record)

    def _csem_url(self, record):
        uid = record.get("uid") or ""
        if not uid:
            slugs = record.get("slugs") or []
            uid = slugs[0] if slugs else ""
        if not uid:
            return ""
        return urljoin(self.base_url, f"/en/press/{uid}/")

    def _external_link(self, data):
        link = data.get("externalLink") or {}
        if isinstance(link, dict):
            return link.get("url") or ""
        return ""

    def _pdf_url(self, data):
        external = self._external_link(data)
        if external and self._PDF_RE.search(urlparse(external).path):
            return external
        found = self._find_pdf_url(data)
        return found or ""

    def _find_pdf_url(self, value):
        if isinstance(value, dict):
            for key in ("url", "href"):
                candidate = value.get(key)
                if isinstance(candidate, str) and self._PDF_RE.search(urlparse(candidate).path):
                    return candidate
            for child in value.values():
                found = self._find_pdf_url(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = self._find_pdf_url(child)
                if found:
                    return found
        return ""

    def _publication_date(self, record):
        data = record.get("data") or {}
        date = data.get("publicationDate") or ""
        if date:
            return date[:10]
        for key in ("first_publication_date", "last_publication_date"):
            raw = record.get(key) or ""
            if len(raw) >= 10:
                return raw[:10]
        return ""

    def _slice_body_text(self, slices):
        parts = []
        for item in slices:
            if not isinstance(item, dict):
                continue
            slice_type = item.get("slice_type") or ""
            if slice_type != "article_paragraph":
                continue
            primary = item.get("primary") or {}
            text = self._rich_text(primary.get("content"))
            if text:
                parts.append(text)
        return "\n\n".join(parts)

    def _rich_text(self, value):
        parts = []
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    text = item.get("text")
                    if text:
                        parts.append(text)
                elif isinstance(item, str):
                    parts.append(item)
        elif isinstance(value, dict):
            text = value.get("text")
            if text:
                parts.append(text)
        elif isinstance(value, str):
            parts.append(value)
        return self._clean_text("\n".join(parts))

    def _taxonomy_labels(self, data, collection_key, item_key):
        labels = []
        for wrapper in data.get(collection_key) or []:
            if not isinstance(wrapper, dict):
                continue
            item = wrapper.get(item_key) or {}
            if not isinstance(item, dict):
                continue
            label = self._link_label(item)
            if label:
                labels.append(label)
        return self._dedupe_keep_order(labels)

    def _keywords(self, data):
        keywords = []
        for collection_key, item_key in (
            ("domains", "domain"),
            ("industries", "industry"),
            ("technicalFocuses", "technicalFocus"),
        ):
            keywords.extend(self._taxonomy_labels(data, collection_key, item_key))

        meta_keywords = self._rich_text(data.get("metaKeywords"))
        if meta_keywords:
            for item in re.split(r"[,;]", meta_keywords):
                cleaned = self._clean_text(item)
                if cleaned:
                    keywords.append(cleaned)
        return self._dedupe_keep_order(keywords)

    def _link_label(self, item):
        tags = item.get("tags") or []
        if tags:
            return self._clean_text(tags[0])
        raw = item.get("uid") or item.get("slug") or item.get("id") or ""
        return self._clean_text(raw.replace("-", " "))

    def _fetch_public_html_text(self, url, item_label):
        if not url or not url.startswith(self.base_url):
            return ""
        raw, _effective_url = self._curl_get(
            url,
            context=f"item {item_label} HTML fallback",
            referer=self.START_URL,
        )
        if not raw:
            return ""
        soup = self._make_soup(raw, context=f"item {item_label} HTML fallback")
        if soup is None:
            return ""
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        main = soup.select_one("main article") or soup.select_one("main") or soup.body
        if not main:
            return ""
        return self._clean_text(main.get_text(" "))

    def _make_soup(self, raw, context="html"):
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                print(f"[{self.site_id}] {context}: BeautifulSoup({parser}) failed: {exc}")
        print(f"[{self.site_id}] {context}: all BeautifulSoup parsers failed")
        return None

    @staticmethod
    def _clean_text(value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\u00a0", " ")
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _dedupe_keep_order(values):
        seen = set()
        result = []
        for value in values:
            key = value.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(value)
        return result

    @staticmethod
    def _dedupe_url(url):
        cleaned = (url or "").strip()
        if not cleaned:
            return ""
        parsed = urlparse(cleaned)
        if not parsed.scheme or not parsed.netloc:
            return cleaned.rstrip("/")
        return parsed._replace(fragment="").geturl().rstrip("/")

    @staticmethod
    def _hash_id(value):
        return hashlib.sha1((value or "").encode("utf-8", errors="replace")).hexdigest()

    def _near_deadline(self, start_time):
        elapsed = time.monotonic() - start_time
        return elapsed >= (self.MAX_WALL_SECONDS - self.WALL_MARGIN_SECONDS)
