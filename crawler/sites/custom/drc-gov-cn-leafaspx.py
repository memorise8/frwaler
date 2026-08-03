# -*- coding: utf-8 -*-
"""Crawler for DRC policy interpretation leaf pages.

The Leaf.aspx shell loads records from /Json/GetPageDocuments.ashx and
individual records from DocView.aspx pages. DRC serves these responses as
GB2312/GBK-like bytes, so this crawler uses curl and byte-level decoding.
"""

import json
import os
import re
import subprocess
import time
import uuid
from html import unescape
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class DrcGovCnLeafAspxCrawler(BaseCrawler):
    site_id = "drc-gov-cn-leafaspx"
    site_name = "Custom: drc-gov-cn-leafaspx"
    base_url = "https://www.drc.gov.cn"

    _START_URL = "https://www.drc.gov.cn/Leaf.aspx?leafid=1338"
    _LIST_URL = "https://www.drc.gov.cn/Json/GetPageDocuments.ashx"
    _CATEGORY = "政策解读"
    _CHANNEL_ID = "378"
    _LEAF_ID = "1338"
    _PAGE_SIZE = 100
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _MIN_ABSTRACT_CHARS = 100

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay

    # ------------------------------------------------------------------
    # Network / decoding helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, referer=None, timeout=45):
        """Fetch a URL with curl, retrying transient network failures."""
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--max-time",
            str(timeout),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "-H",
            "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        waits = (1, 3, 9)
        last_error = None
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 5,
                    check=False,
                )
                if result.returncode == 0 and result.stdout:
                    return self._decode_bytes(result.stdout)
                stderr = self._decode_bytes(result.stderr or b"").strip()
                last_error = stderr or f"curl exit {result.returncode}"
            except Exception as exc:
                last_error = str(exc)

            if attempt < 2:
                wait = waits[attempt]
                print(
                    f"[{self.site_id}] curl failed for {url} "
                    f"(attempt {attempt + 1}/3): {last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    @staticmethod
    def _decode_bytes(raw):
        """Decode mixed Chinese site bytes without raising."""
        if raw is None:
            return ""
        for encoding in ("utf-8-sig", "gb18030", "gbk", "gb2312"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    def _make_soup(self, raw):
        """Build a BeautifulSoup tree with parser fallbacks."""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed: {exc}")
        return BeautifulSoup("", "html.parser")

    @staticmethod
    def _clean_text(value):
        if value is None:
            return ""
        value = unescape(str(value))
        value = value.replace("\xa0", " ").replace("\u3000", " ")
        value = re.sub(r"\s+", " ", value)
        return value.strip()

    @staticmethod
    def _split_people(value):
        value = DrcGovCnLeafAspxCrawler._clean_text(value)
        if not value:
            return []
        parts = re.split(r"[;,；、]+", value)
        parts = [p.strip() for p in parts if p.strip()]
        return parts or [value]

    @staticmethod
    def _split_keywords(value):
        value = DrcGovCnLeafAspxCrawler._clean_text(value)
        if not value:
            return []
        parts = re.split(r"[,，;；、]+", value)
        return [p.strip() for p in parts if p.strip()]

    @staticmethod
    def _date_only(value):
        value = DrcGovCnLeafAspxCrawler._clean_text(value)
        if not value or value.startswith("0001-01-01"):
            return ""
        match = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", value)
        if match:
            year, month, day = match.groups()
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        if re.match(r"^\d{4}-\d{2}-\d{2}", value):
            return value[:10]
        return ""

    def _absolute_url(self, href):
        href = self._clean_text(href)
        if not href or href.lower().startswith(("javascript:", "mailto:")):
            return ""
        return urljoin(self.base_url + "/", href)

    def _meta(self, soup, name):
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content") is not None:
            return self._clean_text(tag.get("content"))
        return ""

    # ------------------------------------------------------------------
    # DRC API / page parsing
    # ------------------------------------------------------------------

    def _list_url(self, page):
        params = {
            "chnid": self._CHANNEL_ID,
            "leafid": self._LEAF_ID,
            "page": str(page),
            "pagesize": str(self._PAGE_SIZE),
            "sublen": "21",
            "sumlen": "230",
            "keyword": "",
            "expertid": "0",
        }
        return f"{self._LIST_URL}?{urlencode(params)}"

    def _fetch_list_page(self, page):
        url = self._list_url(page)
        raw = self._curl_get(url, referer=self._START_URL)
        if not raw:
            return [], None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] invalid JSON on page {page}: {exc}")
            return [], None

        if not isinstance(data, list) or not data:
            return [], None
        block = data[0] if isinstance(data[0], dict) else {}
        rows = block.get("rows") or []
        if not isinstance(rows, list):
            rows = []
        total_page = None
        try:
            total_page = int(block.get("totalpage") or 0)
        except (TypeError, ValueError):
            total_page = None
        return rows, total_page

    def _detail_url_from_row(self, row):
        href = row.get("DocViewUrl") or row.get("OuterUrl") or ""
        return self._absolute_url(href)

    def _external_id(self, row, url):
        doc_id = row.get("DocId")
        if doc_id not in (None, ""):
            return str(doc_id)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        values = query.get("docid") or query.get("DocId")
        if values:
            return str(values[0])
        return url.rstrip("/").split("/")[-1] or url

    def _parse_detail(self, row, detail_url, raw):
        soup = self._make_soup(raw)

        title = (
            self._meta(soup, "ArticleTitle")
            or self._clean_text(row.get("AllSubject"))
            or self._clean_text(row.get("Subject"))
        )
        if not title:
            subject = soup.select_one("#MainContent_docSubject")
            title = self._clean_text(subject.get_text(" ", strip=True) if subject else "")

        published_date = (
            self._date_only(self._meta(soup, "PubDate"))
            or self._date_only(row.get("DelivedDate"))
        )
        author_text = self._meta(soup, "Author") or self._clean_text(row.get("Author"))
        source = self._meta(soup, "ContentSource") or self._clean_text(row.get("Source"))
        keyword_text = self._meta(soup, "Keywords") or self._clean_text(row.get("KeyWords"))

        content_node = soup.select_one("#MainContent_docContent")
        if content_node:
            for node in content_node.select("script, style, noscript"):
                node.decompose()
            detail_text = self._clean_text(content_node.get_text(" ", strip=True))
        else:
            detail_text = ""
        list_summary = self._clean_text(row.get("Summary"))
        abstract = detail_text if len(detail_text) >= len(list_summary) else list_summary

        attachment_urls = []
        attachment_names = []
        pdf_url = ""
        for link in soup.select("#MainContent_docAttachment a[href], a[href]"):
            href = self._absolute_url(link.get("href"))
            if not href:
                continue
            label = self._clean_text(link.get_text(" ", strip=True))
            is_attachment = "DownloadFile.aspx" in href or ".pdf" in href.lower()
            if not is_attachment:
                continue
            attachment_urls.append(href)
            attachment_names.append(label)
            if not pdf_url:
                pdf_url = href

        metadata = {
            "list_api": self._LIST_URL,
            "list_page_params": {
                "chnid": self._CHANNEL_ID,
                "leafid": self._LEAF_ID,
                "pagesize": self._PAGE_SIZE,
            },
            "detail_url": detail_url,
            "raw_row": row,
            "content_source": source,
            "outer_url": self._clean_text(row.get("OuterUrl")),
            "image_src": self._absolute_url(row.get("ImageSrc") or ""),
            "attachment_urls": attachment_urls,
            "attachment_names": attachment_names,
            "journal": source,
        }

        external_id = self._external_id(row, detail_url)
        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.site_id}:{external_id}")),
            "site_id": self.site_id,
            "external_id": external_id,
            "title": title,
            "authors": json.dumps(self._split_people(author_text), ensure_ascii=False),
            "abstract": abstract,
            "category": self._CATEGORY,
            "keywords": json.dumps(self._split_keywords(keyword_text), ensure_ascii=False),
            "published_date": published_date,
            "url": detail_url,
            "pdf_url": pdf_url,
            "doi": "",
            "department": source or "国务院发展研究中心",
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.monotonic()
        saved = 0
        page = 1
        seen_urls = set()
        limit_or_inf = limit if limit is not None else "inf"

        while page <= self._MAX_PAGES:
            if limit is not None and saved >= limit:
                break
            if time.monotonic() - start_time >= self._MAX_SECONDS - 30:
                print(f"[{self.site_id}] approaching 25 minute crawl budget; exiting cleanly")
                break

            rows, total_pages = self._fetch_list_page(page)
            if not rows:
                print(f"[{self.site_id}] page {page}: no rows; stopping")
                break

            new_on_page = 0
            for idx, row in enumerate(rows, start=1):
                if limit is not None and saved >= limit:
                    break
                if time.monotonic() - start_time >= self._MAX_SECONDS - 30:
                    print(f"[{self.site_id}] approaching 25 minute crawl budget; exiting cleanly")
                    return saved

                detail_url = self._detail_url_from_row(row)
                item_label = row.get("DocId") or f"page {page} item {idx}"
                if not detail_url:
                    print(f"[{self.site_id}] item {item_label} skipped: missing detail URL")
                    continue
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                try:
                    if self.detail_delay:
                        time.sleep(self.detail_delay)
                    raw_detail = self._curl_get(detail_url, referer=self._START_URL)
                    if not raw_detail:
                        raise RuntimeError("empty detail response after retries")

                    paper = self._parse_detail(row, detail_url, raw_detail)
                    if not paper.get("title"):
                        print(f"[{self.site_id}] item {item_label} skipped: missing title")
                        continue

                    abstract = self._clean_text(paper.get("abstract"))
                    if len(abstract) < self._MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"short abstract ({len(abstract)} chars)"
                        )
                        continue
                    paper["abstract"] = abstract

                    self._save_paper(paper)
                    saved += 1
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: 0 new records; stopping")
                break
            if total_pages is not None and total_pages > 0 and page >= total_pages:
                print(f"[{self.site_id}] reached final API page {page}/{total_pages}")
                break

            page += 1

        if page > self._MAX_PAGES:
            print(f"[{self.site_id}] reached safety page cap {self._MAX_PAGES}; stopping")

        return saved
