# -*- coding: utf-8 -*-
"""Crawler for NCHA industry standards column.

Target: http://www.ncha.gov.cn/col/col2423/index.html

The visible page is rendered by the common jpage dataproxy endpoint. Records
are table rows whose item link is the standard PDF download endpoint; there is
no separate HTML detail page for this column.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import parse_qs, unquote_plus, urlencode, urljoin, urlparse

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - repo requirements include bs4
    BeautifulSoup = None


class NchaGovCnColCrawler(BaseCrawler):
    site_id = "ncha-gov-cn-col"
    site_name = "Custom: ncha-gov-cn-col"
    base_url = "http://www.ncha.gov.cn"

    START_URL = base_url + "/col/col2423/index.html"
    LIST_API = base_url + "/module/jslib/jquery/jpage/dataproxy.jsp"
    CATEGORY = "行业标准"
    PUBLISHER = "国家文物局"
    COLUMN_ID = "2423"
    UNIT_ID = "18241"
    WEB_ID = "1"

    SAFETY_PAGE_CAP = 200
    WALL_CLOCK_BUDGET_SECONDS = 25 * 60
    CURL_TIMEOUT = 45
    BACKOFF_SECONDS = (1, 3, 9)
    DETAIL_DELAY_SECONDS = 1.0
    MIN_ABSTRACT_CHARS = 50
    _CURL_META_MARKER = "__NCHA_GOV_CN_COL_CURL_META__:"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = self.DETAIL_DELAY_SECONDS if detail_delay is None else detail_delay

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls = set()
        started_at = time.time()
        limit_or_inf = limit if limit is not None else "inf"

        print(f"[{self.site_id}] list endpoint: {self.LIST_API}")

        try:
            while page <= self.SAFETY_PAGE_CAP:
                if limit is not None and saved >= limit:
                    break
                if self._budget_nearly_exhausted(started_at):
                    print(f"[{self.site_id}] approaching 25 minute crawl budget; stopping cleanly")
                    break

                if page == 1 or page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

                list_url = self._list_url(page)
                raw = self._curl_get_text(
                    list_url,
                    context=f"list page {page}",
                    referer=self.START_URL,
                    accept="application/xml,text/xml,text/html,*/*;q=0.8",
                )
                if not raw:
                    print(f"[{self.site_id}] list page {page} failed; stopping")
                    break

                records, total_pages, total_records, has_next = self._parse_list_page(raw, page)
                if page == 1 and total_records is not None:
                    print(f"[{self.site_id}] total records reported by site: {total_records}")
                if not records:
                    print(f"[{self.site_id}] page {page}: 0 records; stopping")
                    break

                new_on_page = 0
                for idx, record in enumerate(records, start=1):
                    if limit is not None and saved >= limit:
                        break
                    if self._budget_nearly_exhausted(started_at):
                        print(f"[{self.site_id}] approaching 25 minute crawl budget; stopping cleanly")
                        return saved

                    item_url = record.get("pdf_url") or record.get("url")
                    item_label = f"page {page} item {idx}"
                    if not item_url:
                        continue
                    if item_url in seen_urls:
                        continue
                    seen_urls.add(item_url)
                    new_on_page += 1

                    try:
                        time.sleep(self.detail_delay)
                        header_text, effective_url = self._curl_get_headers(
                            item_url,
                            context=f"item {item_label} detail",
                            referer=self.START_URL,
                            accept="application/pdf,*/*;q=0.8",
                        )
                        if not header_text:
                            raise RuntimeError("detail header fetch failed after retries")

                        parsed = self._enrich_record_with_headers(
                            record=record,
                            headers_text=header_text,
                            effective_url=effective_url or item_url,
                        )
                        abstract = parsed.get("abstract") or ""
                        if len(abstract) < self.MIN_ABSTRACT_CHARS:
                            print(
                                f"[{self.site_id}] item {item_label} skipped: "
                                f"abstract too short ({len(abstract)} chars)"
                            )
                            continue

                        self._save_paper(self._to_paper(parsed))
                        saved += 1
                        print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {parsed['title'][:90]}")
                    except Exception as exc:
                        print(f"[{self.site_id}] item {item_label} failed: {exc}")
                        continue

                if new_on_page == 0:
                    print(f"[{self.site_id}] page {page}: 0 new records; stopping")
                    break

                if total_pages is not None and page >= total_pages:
                    break
                if not has_next and total_pages is None:
                    print(f"[{self.site_id}] page {page}: next page link absent; stopping")
                    break

                page += 1

            if page >= self.SAFETY_PAGE_CAP:
                print(f"[{self.site_id}] reached safety cap of {self.SAFETY_PAGE_CAP} pages")
        except KeyboardInterrupt:
            print(f"[{self.site_id}] interrupted by user")
            raise

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _list_url(self, page):
        params = {
            "page": int(page),
            "appid": "1",
            "webid": self.WEB_ID,
            "path": "/",
            "columnid": self.COLUMN_ID,
            "unitid": self.UNIT_ID,
            "webname": self.PUBLISHER,
            "permissiontype": "0",
        }
        return f"{self.LIST_API}?{urlencode(params)}"

    def _curl_get_text(self, url, context="request", referer=None, accept=None):
        body, _headers, effective_url = self._curl(
            url,
            method="GET",
            context=context,
            referer=referer,
            accept=accept,
        )
        if body is None:
            return None
        return self._decode_bytes(body)

    def _curl_get_headers(self, url, context="request", referer=None, accept=None):
        _body, headers, effective_url = self._curl(
            url,
            method="HEAD",
            context=context,
            referer=referer,
            accept=accept,
        )
        if headers is None:
            return None, effective_url
        return self._decode_bytes(headers), effective_url

    def _curl(self, url, method="GET", context="request", referer=None, accept=None):
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
            "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
            "-w",
            "\n" + self._CURL_META_MARKER + "%{http_code}\t%{url_effective}",
        ]
        if method == "HEAD":
            cmd.append("-I")
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
                stdout = result.stdout or b""
                body_or_headers, http_code, effective_url = self._split_curl_output(stdout, url)
                stderr = self._decode_bytes(result.stderr or b"").strip()

                if result.returncode != 0:
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if http_code and http_code.isdigit() and int(http_code) >= 400:
                    raise RuntimeError(f"HTTP {http_code} for {effective_url}")
                if not body_or_headers:
                    raise RuntimeError(f"empty response for {effective_url}")

                if method == "HEAD":
                    return b"", body_or_headers, effective_url
                return body_or_headers, b"", effective_url
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
        return None, None, url

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

    @staticmethod
    def _decode_bytes(raw):
        if raw is None:
            return ""
        for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, raw, page):
        total_pages = self._int_from_tag(raw, "totalpage")
        total_records = self._int_from_tag(raw, "totalrecord")
        has_next = bool(re.search(r"<nextgroup><!\[CDATA\[.*?href=", raw, re.DOTALL | re.I))
        record_htmls = re.findall(r"<record><!\[CDATA\[(.*?)\]\]></record>", raw, re.DOTALL)

        records = []
        for raw_record in record_htmls:
            record = self._parse_record(raw_record, page)
            if record:
                records.append(record)
        return records, total_pages, total_records, has_next

    @staticmethod
    def _int_from_tag(raw, tag):
        match = re.search(rf"<{tag}>\s*(\d+)\s*</{tag}>", raw or "", re.I)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    def _parse_record(self, row_html, page):
        soup = self._make_soup(
            f"<table><tbody>{row_html}</tbody></table>",
            context=f"list page {page} record",
        )
        if soup is None:
            return None

        cells = soup.select("tr.me td")
        if not cells:
            cells = soup.select("td")
        if len(cells) < 8:
            return None

        standard_number = self._clean_text(cells[1].get_text(" ", strip=True))
        link = cells[2].find("a", href=True)
        title = self._clean_text(link.get_text(" ", strip=True) if link else cells[2].get_text(" ", strip=True))
        href = link.get("href") if link else ""
        pdf_url = self._absolute_url(href)
        release_raw = self._clean_text(cells[3].get_text(" ", strip=True))
        effective_raw = self._clean_text(cells[4].get_text(" ", strip=True))
        standard_nature = self._clean_text(cells[5].get_text(" ", strip=True))
        standard_status = self._clean_text(cells[6].get_text(" ", strip=True))
        publisher = self._clean_text(cells[7].get_text(" ", strip=True)) or self.PUBLISHER

        download_filename = self._query_filename(pdf_url)
        native_number = self._native_number(download_filename, standard_number, pdf_url)
        post_number = self._post_number(download_filename, native_number)
        published_date = self._date_only(release_raw)
        listed_date = published_date
        effective_date = self._date_only(effective_raw)
        abstract = self._build_abstract(
            title=title,
            standard_number=standard_number,
            published_date=published_date,
            effective_date=effective_date,
            standard_nature=standard_nature,
            standard_status=standard_status,
            publisher=publisher,
        )

        return {
            "id": native_number,
            "site_id": self.site_id,
            "external_id": native_number,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": None,
            "publisher": publisher,
            "department": None,
            "journal": None,
            "url": pdf_url,
            "pdf_url": pdf_url,
            "keywords": self._keywords([self.CATEGORY, "文物", "行业标准", standard_nature, standard_status]),
            "category": self.CATEGORY,
            "doi": None,
            "original_filename": None,
            "download_filename": download_filename,
            "standard_number": standard_number,
            "standard_nature": standard_nature,
            "standard_status": standard_status,
            "effective_date": effective_date,
            "release_date_raw": release_raw,
            "effective_date_raw": effective_raw,
            "list_page": page,
            "raw_record_html": row_html,
        }

    def _enrich_record_with_headers(self, record, headers_text, effective_url):
        parsed = dict(record)
        content_disposition = self._header_value(headers_text, "content-disposition")
        original_filename = (
            self._filename_from_content_disposition(content_disposition)
            or parsed.get("download_filename")
            or self._filename_from_url(parsed.get("pdf_url"))
        )
        parsed["original_filename"] = original_filename
        parsed["url"] = effective_url or parsed.get("url")
        parsed["pdf_url"] = effective_url or parsed.get("pdf_url")
        parsed["headers"] = {
            "content_disposition": content_disposition,
            "content_type": self._header_value(headers_text, "content-type"),
            "content_length": self._header_value(headers_text, "content-length"),
            "last_modified": self._header_value(headers_text, "last-modified"),
            "etag": self._header_value(headers_text, "etag"),
        }
        return parsed

    def _to_paper(self, parsed):
        metadata = {
            "posted_date": parsed.get("release_date_raw") or parsed.get("listed_date"),
            "listed_date": parsed.get("listed_date"),
            "originalFilename": parsed.get("original_filename"),
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "node_id": self.COLUMN_ID,
            "columnid": self.COLUMN_ID,
            "unitid": self.UNIT_ID,
            "webid": self.WEB_ID,
            "list_api_endpoint": self.LIST_API,
            "source_column_url": self.START_URL,
            "list_page": parsed.get("list_page"),
            "standard_number": parsed.get("standard_number"),
            "standard_nature": parsed.get("standard_nature"),
            "standard_status": parsed.get("standard_status"),
            "effective_date": parsed.get("effective_date"),
            "effective_date_raw": parsed.get("effective_date_raw"),
            "download_filename": parsed.get("download_filename"),
            "post_number": parsed.get("post_number"),
            "headers": parsed.get("headers"),
            "raw_record_html": parsed.get("raw_record_html"),
        }
        return {
            "id": parsed.get("id"),
            "site_id": self.site_id,
            "external_id": parsed.get("external_id"),
            "post_number": parsed.get("post_number"),
            "title": parsed.get("title"),
            "abstract": parsed.get("abstract"),
            "published_date": parsed.get("published_date"),
            "listed_date": parsed.get("listed_date"),
            "posted_date": parsed.get("listed_date"),
            "authors": parsed.get("authors"),
            "publisher": parsed.get("publisher"),
            "department": parsed.get("department"),
            "journal": parsed.get("journal"),
            "url": parsed.get("url"),
            "pdf_url": parsed.get("pdf_url"),
            "keywords": parsed.get("keywords"),
            "category": parsed.get("category"),
            "doi": parsed.get("doi"),
            "original_filename": parsed.get("original_filename"),
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _make_soup(self, raw, context="HTML"):
        if BeautifulSoup is None:
            return None
        text = self._decode_bytes(raw) if isinstance(raw, bytes) else (raw or "")
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                last_exc = exc
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        print(f"[{self.site_id}] all BeautifulSoup parsers failed for {context}: {last_exc}")
        return None

    def _build_abstract(
        self,
        title,
        standard_number,
        published_date,
        effective_date,
        standard_nature,
        standard_status,
        publisher,
    ):
        parts = [
            f"{publisher or self.PUBLISHER}{self.CATEGORY}《{title}》",
            f"标准号为{standard_number}" if standard_number else "",
            f"发布日期为{published_date}" if published_date else "",
            f"实施日期为{effective_date}" if effective_date else "",
            f"标准性质为{standard_nature}" if standard_nature else "",
            f"当前状态为{standard_status}" if standard_status else "",
            "该记录来自国家文物局政府信息公开行业标准栏目，原文以PDF附件形式公开，适用于文物保护、博物馆管理、考古现场和文化遗产保护相关规范查询",
        ]
        return "；".join(part for part in parts if part) + "。"

    @staticmethod
    def _clean_text(value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _absolute_url(self, href):
        href = self._clean_text(href)
        if not href:
            return None
        return urljoin(self.base_url + "/", href)

    @staticmethod
    def _date_only(value):
        match = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", value or "")
        if not match:
            return None
        year, month, day = match.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"

    @staticmethod
    def _query_filename(url):
        if not url:
            return None
        parsed = urlparse(url)
        values = parse_qs(parsed.query).get("filename")
        if values and values[0]:
            return unquote_plus(values[0])
        return None

    @staticmethod
    def _native_number(download_filename, standard_number, url):
        if download_filename:
            stem = download_filename.rsplit(".", 1)[0]
            if stem:
                return stem
        if standard_number:
            slug = re.sub(r"[^0-9A-Za-z]+", "-", standard_number).strip("-")
            if slug:
                return slug
        if url:
            tail = url.rstrip("/").split("/")[-1].split("?")[0]
            if tail:
                return tail
        return None

    @staticmethod
    def _post_number(download_filename, fallback):
        if download_filename:
            stem = download_filename.rsplit(".", 1)[0]
            if re.fullmatch(r"\d+", stem or ""):
                return stem
        if fallback:
            digits = re.sub(r"\D+", "", fallback)
            if digits:
                return digits
            return fallback
        return None

    @staticmethod
    def _keywords(values):
        seen = []
        for value in values:
            value = (value or "").strip()
            if value and value not in seen:
                seen.append(value)
        return ",".join(seen) if seen else None

    @staticmethod
    def _header_value(headers_text, name):
        found = None
        prefix = name.lower() + ":"
        for line in (headers_text or "").splitlines():
            line = line.strip()
            if line.lower().startswith(prefix):
                found = line.split(":", 1)[1].strip()
        return found

    @staticmethod
    def _filename_from_content_disposition(value):
        if not value:
            return None
        match = re.search(r"filename\*\s*=\s*([^;]+)", value, re.I)
        if match:
            raw = match.group(1).strip().strip("\"'")
            if "''" in raw:
                raw = raw.split("''", 1)[1]
            return unquote_plus(raw)
        match = re.search(r"filename\s*=\s*(\"[^\"]+\"|[^;]+)", value, re.I)
        if match:
            raw = match.group(1).strip().strip("\"'")
            return unquote_plus(raw)
        return None

    @staticmethod
    def _filename_from_url(url):
        if not url:
            return None
        tail = url.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
        return unquote_plus(tail) if "." in tail else None

    def _budget_nearly_exhausted(self, started_at):
        return (time.time() - started_at) >= (self.WALL_CLOCK_BUDGET_SECONDS - 30)


Crawler = NchaGovCnColCrawler
