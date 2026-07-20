# -*- coding: utf-8 -*-
"""Crawler for BfR English annual reports."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class BfrBundDeEnCrawler(BaseCrawler):
    site_id = "bfr-bund-de-en"
    site_name = "Custom: bfr-bund-de-en"
    base_url = "https://www.bfr.bund.de"

    START_URL = "https://www.bfr.bund.de/en/publications/annual-reports/"
    FALLBACK_API_ENDPOINT = "https://www.bfr.bund.de/api/v1/en/mini-suche"
    FALLBACK_RESTRICTION_FILTERS = ["folder:117", "folder:1179", "folder:3746"]

    BACKOFF_SECONDS = (1, 3, 9)
    CURL_TIMEOUT = 60
    PDF_TEXT_TIMEOUT = 45
    PDF_TEXT_PAGES = 8
    MIN_ABSTRACT_CHARS = 50
    PREFERRED_ABSTRACT_CHARS = 100
    MAX_ABSTRACT_CHARS = 5000
    _CURL_META_MARKER = "__BFR_BUND_DE_EN_CURL_META__:"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    def crawl(self, limit=None):
        """Crawl BfR annual report records from the TYPO3/Solr JSON API."""
        saved = 0
        item_number = 0
        seen_external_ids = set()

        start_raw, _ = self._curl_get_text(
            self.START_URL,
            context="start page",
            referer=self.base_url + "/en/",
        )
        if not start_raw:
            print(f"[{self.site_id}] start page fetch failed")
            return saved

        start_soup = self._make_soup(start_raw, context="start page")
        if start_soup is None:
            print(f"[{self.site_id}] start page could not be parsed")
            return saved

        api_endpoint, restriction_filters = self._discover_search_config(start_soup)
        print(
            f"[{self.site_id}] list endpoint: {api_endpoint} "
            f"filters={restriction_filters}"
        )

        page = 1
        while True:
            if limit is not None and saved >= limit:
                break

            data = self._fetch_list_page(api_endpoint, restriction_filters, page)
            if data is None:
                if page == 1:
                    print(f"[{self.site_id}] list API failed on first page")
                else:
                    print(f"[{self.site_id}] list API failed on page {page}; stopping")
                break

            records = self._parse_list_records(data)
            pagination = data.get("pagination") if isinstance(data, dict) else {}
            number_of_pages = self._safe_int(
                (pagination or {}).get("numberOfPages"),
                default=page,
            )
            print(
                f"[{self.site_id}] list page {page}/{number_of_pages}: "
                f"discovered {len(records)} records"
            )

            if not records:
                break

            for record in records:
                if limit is not None and saved >= limit:
                    break

                item_number += 1
                try:
                    url = record.get("url") or ""
                    pdf_url = record.get("pdf_url") or ""
                    title = record.get("title") or ""
                    if not url and not pdf_url:
                        raise RuntimeError("record has neither detail URL nor PDF URL")
                    if not title:
                        raise RuntimeError("record has no title")

                    external_id = record.get("external_id") or self._external_id(url, pdf_url)
                    if external_id in seen_external_ids:
                        continue
                    seen_external_ids.add(external_id)

                    time.sleep(self.detail_delay)
                    detail_soup = None
                    detail_effective_url = url
                    if url:
                        detail_raw, detail_effective_url = self._curl_get_text(
                            url,
                            context=f"item {item_number} detail",
                            referer=self.START_URL,
                        )
                        if not detail_raw:
                            raise RuntimeError("detail fetch failed after retries")
                        detail_soup = self._make_soup(
                            detail_raw,
                            context=f"item {item_number} detail",
                        )
                        if detail_soup is None:
                            raise RuntimeError("detail HTML could not be parsed")

                    parsed = self._parse_detail(detail_soup, record)
                    abstract = parsed.get("abstract") or ""
                    pdf_text = ""
                    abstract_source = parsed.get("abstract_source") or "detail"

                    if len(abstract) < self.PREFERRED_ABSTRACT_CHARS and pdf_url:
                        pdf_bytes, effective_pdf_url = self._curl_get_bytes(
                            pdf_url,
                            context=f"item {item_number} PDF",
                            referer=detail_effective_url or self.START_URL,
                            accept="application/pdf,*/*;q=0.8",
                        )
                        if not pdf_bytes:
                            raise RuntimeError("PDF fetch failed after retries")
                        parsed["pdf_url"] = effective_pdf_url or pdf_url
                        pdf_text = self._extract_pdf_text(
                            pdf_bytes,
                            context=f"item {item_number} PDF",
                        )
                        pdf_abstract = self._abstract_from_pdf_text(
                            pdf_text,
                            parsed.get("meta_description") or "",
                        )
                        if len(pdf_abstract) > len(abstract):
                            abstract = pdf_abstract
                            abstract_source = "pdf_text"

                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_number} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue
                    if len(abstract) < self.PREFERRED_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_number} skipped: "
                            f"abstract below preferred length ({len(abstract)} chars)"
                        )
                        continue

                    doi = parsed.get("doi") or self._extract_doi(pdf_text)
                    authors = parsed.get("authors") or self._extract_authors(pdf_text)
                    keywords = parsed.get("keywords") or []
                    category = parsed.get("category") or record.get("category") or "Annual reports"
                    if category and category not in keywords:
                        keywords.append(category)

                    metadata = {
                        "source": "TYPO3 Solr mini-suche JSON API",
                        "api_endpoint": api_endpoint,
                        "restriction_filters": restriction_filters,
                        "list_record": record.get("raw") or {},
                        "detail_url": detail_effective_url or url,
                        "abstract_source": abstract_source,
                        "meta_description": parsed.get("meta_description") or "",
                        "isbn": parsed.get("isbn") or "",
                        "issn": parsed.get("issn") or "",
                        "download": record.get("download") or {},
                    }

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "title": parsed.get("title") or title,
                        "authors": json.dumps(authors, ensure_ascii=False),
                        "abstract": abstract,
                        "category": category,
                        "keywords": json.dumps(keywords, ensure_ascii=False),
                        "published_date": parsed.get("published_date") or record.get("published_date") or "",
                        "url": url,
                        "pdf_url": parsed.get("pdf_url") or pdf_url,
                        "doi": doi,
                        "department": "German Federal Institute for Risk Assessment (BfR)",
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {paper['title'][:90]}")
                except Exception as exc:
                    print(f"[bfr-bund-de-en] item {item_number} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break
            if page >= number_of_pages:
                break
            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _fetch_list_page(self, api_endpoint, restriction_filters, page):
        params = []
        for value in restriction_filters:
            params.append(("tx_solr[filter][]", value))
        if page and int(page) > 1:
            params.append(("tx_solr[page]", str(page)))

        url = api_endpoint
        if params:
            url = f"{api_endpoint}?{urlencode(params)}"

        raw, _ = self._curl_get_text(
            url,
            context=f"list page {page}",
            referer=self.START_URL,
            accept="application/json,*/*;q=0.8",
        )
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] invalid list JSON on page {page}: {exc}")
            return None

        try:
            return payload["content"]["colPos0"][0]["content"]["data"]
        except (KeyError, IndexError, TypeError) as exc:
            print(f"[{self.site_id}] unexpected list JSON shape on page {page}: {exc}")
            return None

    def _curl_get_text(self, url, context="request", referer=None, accept=None):
        body, effective_url = self._curl_get_bytes(
            url,
            context=context,
            referer=referer,
            accept=accept
            or (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "application/json,*/*;q=0.8"
            ),
        )
        if body is None:
            return None, effective_url
        return body.decode("utf-8", errors="replace"), effective_url

    def _curl_get_bytes(self, url, context="request", referer=None, accept=None):
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
            f"Accept: {accept or '*/*'}",
            "-H",
            "Accept-Language: en-GB,en;q=0.9,de;q=0.6,*;q=0.3",
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
                body, http_code, effective_url = self._split_curl_output(
                    result.stdout or b"",
                    url,
                )
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                if result.returncode != 0:
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if http_code and http_code.isdigit() and int(http_code) >= 400:
                    raise RuntimeError(f"HTTP {http_code} for {effective_url}")
                if not body:
                    raise RuntimeError(f"empty response for {effective_url}")
                return body, effective_url
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
        marker = ("\n" + self._CURL_META_MARKER).encode("ascii")
        marker_pos = raw.rfind(marker)
        if marker_pos == -1:
            return raw, "", fallback_url
        body = raw[:marker_pos]
        meta = raw[marker_pos + len(marker):].decode("utf-8", errors="replace").strip()
        if "\t" not in meta:
            return body, "", fallback_url
        http_code, effective_url = meta.split("\t", 1)
        return body, http_code.strip(), effective_url.strip() or fallback_url

    # ------------------------------------------------------------------
    # Discovery and parsing
    # ------------------------------------------------------------------

    def _discover_search_config(self, soup):
        app = soup.select_one("#search-app[data-endpoint]")
        endpoint = self.FALLBACK_API_ENDPOINT
        filters = list(self.FALLBACK_RESTRICTION_FILTERS)

        if app is not None:
            endpoint = app.get("data-endpoint") or endpoint
            raw_filters = app.get("data-restriction-filters") or ""
            if raw_filters:
                raw_filters = raw_filters.replace("&quot;", '"')
                try:
                    parsed = json.loads(raw_filters)
                    if isinstance(parsed, list) and parsed:
                        filters = [str(item) for item in parsed if str(item).strip()]
                except (TypeError, ValueError) as exc:
                    print(f"[{self.site_id}] could not parse restriction filters: {exc}")

        endpoint = urljoin(self.base_url + "/", endpoint)
        return endpoint, filters

    def _parse_list_records(self, data):
        docs = (((data or {}).get("documents") or {}).get("list") or {}).get("results") or []
        records = []
        for item in docs:
            if not isinstance(item, dict):
                continue

            download = item.get("download") if isinstance(item.get("download"), dict) else {}
            pdf_url = download.get("url") or ""
            url = item.get("url") or ""
            title = self._clean_text(item.get("title") or "")
            uid = item.get("uid")
            record = {
                "external_id": str(uid) if uid not in (None, "") else "",
                "title": title,
                "published_date": self._parse_date(item.get("publishingDate") or ""),
                "url": urljoin(self.base_url + "/", url) if url else "",
                "pdf_url": urljoin(self.base_url + "/", pdf_url) if pdf_url else "",
                "category": self._clean_text(item.get("groupingName") or item.get("description") or ""),
                "description": self._clean_text(item.get("description") or ""),
                "teaser": self._clean_text(item.get("teaser") or ""),
                "download": download,
                "raw": item,
            }
            if record["title"] and (record["url"] or record["pdf_url"]):
                records.append(record)
        return records

    def _parse_detail(self, soup, record):
        title = record.get("title") or ""
        published_date = record.get("published_date") or ""
        category = record.get("category") or ""
        pdf_url = record.get("pdf_url") or ""
        meta_description = ""
        isbn = ""
        issn = ""
        abstract_parts = []

        if soup is not None:
            h1 = soup.select_one("h1")
            page_title = self._clean_text(h1) if h1 is not None else ""
            if page_title and len(page_title) < 300:
                title = self._dedupe_abbreviation_expansion(page_title)

            meta = soup.find("meta", attrs={"name": "description"})
            if meta is not None:
                meta_description = self._clean_text(meta.get("content") or "")
                if meta_description:
                    abstract_parts.append(meta_description)

            time_el = soup.select_one("time.main-header-detail__meta-date")
            if time_el is not None:
                parsed_date = self._parse_date(
                    time_el.get("datetime") or self._clean_text(time_el)
                )
                if parsed_date:
                    published_date = parsed_date

            cat_el = soup.select_one(".meta-category__label")
            detail_category = self._clean_text(cat_el) if cat_el is not None else ""
            if detail_category:
                category = self._dedupe_abbreviation_expansion(detail_category)

            for item in soup.select(".main-header-detail__meta-publication .meta-info__item"):
                heading = self._clean_text(item.select_one("dt")).lower()
                value = self._clean_text(item.select_one("dd"))
                if heading == "isbn":
                    isbn = value.strip(" /")
                elif heading == "issn":
                    issn = value.strip(" /")

            detail_pdf = self._find_pdf_url(soup)
            if detail_pdf:
                pdf_url = detail_pdf

            for selector in (
                ".text-media__text",
                ".main-header-detail__intro",
                ".main-header-detail__teaser",
            ):
                for el in soup.select(selector):
                    text = self._clean_text(el)
                    if text and text not in abstract_parts:
                        abstract_parts.append(text)

        if not abstract_parts and record.get("teaser"):
            abstract_parts.append(record["teaser"])

        return {
            "title": title,
            "published_date": published_date,
            "category": category,
            "pdf_url": pdf_url,
            "abstract": self._truncate(self._clean_text(" ".join(abstract_parts))),
            "abstract_source": "detail_meta" if abstract_parts else "",
            "meta_description": meta_description,
            "isbn": isbn,
            "issn": issn,
            "doi": "",
            "authors": [],
            "keywords": [x for x in (category, isbn, issn) if x],
        }

    def _find_pdf_url(self, soup):
        for link in soup.select('a[href$=".pdf"], a[href*=".pdf?"]'):
            href = link.get("href") or ""
            if href:
                return urljoin(self.base_url + "/", href)
        return ""

    # ------------------------------------------------------------------
    # PDF helpers
    # ------------------------------------------------------------------

    def _extract_pdf_text(self, pdf_bytes, context="PDF"):
        cmd = [
            "pdftotext",
            "-f",
            "1",
            "-l",
            str(self.PDF_TEXT_PAGES),
            "-layout",
            "-enc",
            "UTF-8",
            "-",
            "-",
        ]
        try:
            result = subprocess.run(
                cmd,
                input=pdf_bytes,
                capture_output=True,
                timeout=self.PDF_TEXT_TIMEOUT,
            )
        except FileNotFoundError:
            print(f"[{self.site_id}] pdftotext not available for {context}")
            return ""
        except Exception as exc:
            print(f"[{self.site_id}] pdftotext failed for {context}: {exc}")
            return ""

        if result.returncode != 0:
            err = (result.stderr or b"").decode("utf-8", errors="replace").strip()
            print(f"[{self.site_id}] pdftotext failed for {context}: {err}")
            return ""
        return (result.stdout or b"").decode("utf-8", errors="replace")

    def _abstract_from_pdf_text(self, pdf_text, prefix=""):
        cleaned = self._clean_pdf_text(pdf_text)
        parts = []
        if prefix:
            parts.append(prefix)
        if cleaned:
            parts.append(cleaned)
        return self._truncate(self._clean_text(" ".join(parts)))

    def _clean_pdf_text(self, text):
        if not text:
            return ""
        text = text.replace("\x0c", "\n")
        lines = []
        for raw_line in text.splitlines():
            line = self._clean_text(raw_line)
            if not line:
                continue
            low = line.lower()
            if low in {"imprint", "contents", "table of contents"}:
                continue
            if low.startswith("download as a free pdf"):
                continue
            lines.append(line)
        return self._clean_text(" ".join(lines))

    def _extract_doi(self, text):
        if not text:
            return ""
        match = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", text, re.IGNORECASE)
        return match.group(0).rstrip(".,;") if match else ""

    def _extract_authors(self, text):
        if not text:
            return []
        match = re.search(
            r"Authors:\s*(.+?)(?:\n\s*\n|Publisher:|Regulatory authority:|Berlin\s+\d{4})",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return []
        block = self._clean_text(match.group(1))
        block = re.sub(r"\bDr\s+", "Dr ", block)
        authors = [part.strip() for part in re.split(r",|;|\band\b", block) if part.strip()]
        return authors[:20]

    # ------------------------------------------------------------------
    # Generic helpers
    # ------------------------------------------------------------------

    def _make_soup(self, raw, context="HTML"):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                last_exc = exc
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        print(f"[{self.site_id}] all BeautifulSoup parsers failed for {context}: {last_exc}")
        return None

    def _parse_date(self, value):
        value = self._clean_text(value)
        if not value:
            return ""
        match = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", value)
        if match:
            day, month, year = match.groups()
            return f"{year}-{int(month):02d}-{int(day):02d}"
        match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", value)
        if match:
            first, second, third = match.groups()
            if int(second) > 12 and int(third) <= 12:
                return f"{first}-{int(third):02d}-{int(second):02d}"
            return f"{first}-{int(second):02d}-{int(third):02d}"
        return value

    def _external_id(self, url, pdf_url):
        key = (url or pdf_url or "").encode("utf-8", errors="replace")
        return hashlib.sha1(key).hexdigest()[:20]

    def _dedupe_abbreviation_expansion(self, text):
        text = self._clean_text(text)
        text = text.replace("BfR short for German Federal Institute for Risk Assessment", "BfR")
        text = text.replace("EFSA short for European Food Safety Authority", "EFSA")
        return self._clean_text(text)

    def _clean_text(self, value):
        if value is None:
            return ""
        if hasattr(value, "get_text"):
            value = value.get_text(" ", strip=True)
        text = unescape(str(value))
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _truncate(self, text):
        text = self._clean_text(text)
        if len(text) <= self.MAX_ABSTRACT_CHARS:
            return text
        return text[: self.MAX_ABSTRACT_CHARS].rsplit(" ", 1)[0].rstrip(".,; ") + "."

    def _safe_int(self, value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
