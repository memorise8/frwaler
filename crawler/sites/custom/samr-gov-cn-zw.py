# -*- coding: utf-8 -*-
"""Crawler for SAMR policy interpretation articles.

The visible column page is a shell. Its list is produced by the JPaas
``front/page/build/unit`` endpoint, which returns JSON containing an HTML
fragment. Article details are public HTML pages with government metadata in
meta tags and the visible information table.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class SamrGovCnZwCrawler(BaseCrawler):
    site_id = "samr-gov-cn-zw"
    site_name = "Custom: samr-gov-cn-zw"
    base_url = "https://www.samr.gov.cn"

    START_URL = "https://www.samr.gov.cn/zw/zjwj/zcjd/index.html"
    LIST_API = base_url + "/api-gateway/jpaas-publish-server/front/page/build/unit"
    CATEGORY = "政策解读"

    WEB_ID = "29e9522dc89d4e088a953d8cede72f4c"
    TPL_SET_ID = "5c30fb89ae5e48b9aefe3cdf49853830"
    COLUMN_PAGE_ID = "f6a995f86b4f4362836c3c2d7ce72889"
    COLUMN_TAG_ID = "内容区域"

    PAGE_SIZE = 20
    SAFETY_PAGE_CAP = 200
    WALL_CLOCK_BUDGET_SECONDS = 25 * 60
    CURL_TIMEOUT = 45
    BACKOFF_SECONDS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 50
    TARGET_ABSTRACT_CHARS = 100
    _CURL_META_MARKER = "__SAMR_GOV_CN_ZW_CURL_META__:"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    def crawl(self, limit=None):
        """Crawl the SAMR policy interpretation list and article pages."""
        saved = 0
        page = 1
        seen_urls = set()
        started_at = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

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

                list_payload = self._fetch_list_page(page)
                if list_payload is None:
                    print(f"[{self.site_id}] list page {page} failed; stopping")
                    break

                records, total_count = self._parse_list_records(list_payload, page)
                if total_count is not None and page == 1:
                    print(f"[{self.site_id}] total records reported by site: {total_count}")
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

                    detail_url = record.get("url") or ""
                    item_label = f"page {page} item {idx}"
                    if not detail_url:
                        continue
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_on_page += 1

                    try:
                        time.sleep(self.detail_delay)
                        detail_raw, effective_url = self._curl_get_text(
                            detail_url,
                            context=f"item {item_label} detail",
                            referer=self.START_URL,
                            accept=(
                                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                                "application/json,*/*;q=0.8"
                            ),
                        )
                        if not detail_raw:
                            raise RuntimeError("detail fetch failed after retries")

                        detail_soup = self._make_soup(
                            detail_raw,
                            context=f"item {item_label} detail",
                        )
                        if detail_soup is None:
                            raise RuntimeError("detail HTML could not be parsed")

                        parsed = self._parse_detail(
                            record=record,
                            soup=detail_soup,
                            raw_html=detail_raw,
                            effective_url=effective_url or detail_url,
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

                if total_count is not None and page * self.PAGE_SIZE >= total_count:
                    break

                page += 1

            if page > self.SAFETY_PAGE_CAP:
                print(f"[{self.site_id}] reached safety cap of {self.SAFETY_PAGE_CAP} pages")
        except KeyboardInterrupt:
            print(f"[{self.site_id}] interrupted by user")
            raise

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page):
        param_json = json.dumps(
            {"pageNo": int(page), "pageSize": self.PAGE_SIZE},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        params = {
            "parseType": "bulidstatic",
            "webId": self.WEB_ID,
            "tplSetId": self.TPL_SET_ID,
            "pageType": "column",
            "tagId": self.COLUMN_TAG_ID,
            "editType": "null",
            "pageId": self.COLUMN_PAGE_ID,
            "paramJson": param_json,
        }
        url = f"{self.LIST_API}?{urlencode(params)}"
        raw, _ = self._curl_get_text(
            url,
            context=f"list page {page}",
            referer=self.START_URL,
            accept="application/json, text/javascript, */*; q=0.01",
            extra_headers=["X-Requested-With: XMLHttpRequest"],
        )
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] invalid list JSON on page {page}: {exc}")
            return None
        if not isinstance(payload, dict) or not payload.get("success"):
            print(f"[{self.site_id}] unexpected list response on page {page}")
            return None
        html = (payload.get("data") or {}).get("html") or ""
        if not html:
            return ""
        return html

    def _fetch_article_unit_html(self, page_id, referer):
        if not page_id:
            return ""
        params = {
            "parseType": "bulid",
            "webId": self.WEB_ID,
            "tplSetId": self.TPL_SET_ID,
            "pageType": "article",
            "tagId": "文章正文2",
            "editType": "null",
            "pageId": page_id,
        }
        url = f"{self.LIST_API}?{urlencode(params)}"
        raw, _ = self._curl_get_text(
            url,
            context=f"article unit {page_id}",
            referer=referer or self.START_URL,
            accept="application/json, text/javascript, */*; q=0.01",
            extra_headers=["X-Requested-With: XMLHttpRequest"],
        )
        if not raw:
            return ""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return ""
        if not isinstance(payload, dict) or not payload.get("success"):
            return ""
        return (payload.get("data") or {}).get("html") or ""

    def _curl_get_text(self, url, context="request", referer=None, accept=None, extra_headers=None):
        body, effective_url = self._curl_get_bytes(
            url,
            context=context,
            referer=referer,
            accept=accept,
            extra_headers=extra_headers,
        )
        if body is None:
            return None, effective_url
        return self._decode_bytes(body), effective_url

    def _curl_get_bytes(self, url, context="request", referer=None, accept=None, extra_headers=None):
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
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        for header in extra_headers or []:
            cmd.extend(["-H", header])
        cmd.append(url)

        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.CURL_TIMEOUT + 10,
                )
                body, http_code, effective_url = self._split_curl_output(result.stdout or b"", url)
                stderr = self._decode_bytes(result.stderr or b"").strip()

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
    # Parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, raw, context="HTML"):
        if isinstance(raw, bytes):
            raw = self._decode_bytes(raw)
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

    def _parse_list_records(self, html, page):
        soup = self._make_soup(html, context=f"list page {page}")
        if soup is None:
            return [], None

        total_count = None
        pagination = soup.select_one(".pagination")
        if pagination and pagination.get("count"):
            try:
                total_count = int(pagination.get("count"))
            except (TypeError, ValueError):
                total_count = None

        records = []
        for a_tag in soup.select("li.nav04Left02_content a[href]"):
            href = self._clean_text(a_tag.get("href"))
            url = self._absolute_url(href)
            if not url:
                continue
            title = self._clean_text(a_tag.get("title") or a_tag.get_text(" ", strip=True))
            row = a_tag.find_parent("ul")
            listed_raw = ""
            if row:
                date_tag = row.select_one("li.nav04Left02_contenttime")
                listed_raw = self._clean_text(date_tag.get_text(" ", strip=True) if date_tag else "")
            listed_date = self._date_only(listed_raw)
            page_id = self._page_id_from_url(url)
            records.append(
                {
                    "title": title,
                    "url": url,
                    "listed_date": listed_date,
                    "listed_date_raw": listed_raw,
                    "page_id": page_id,
                    "list_page": page,
                    "raw": {
                        "href": href,
                        "title": title,
                        "listed_date": listed_raw,
                    },
                }
            )

        return records, total_count

    def _parse_detail(self, record, soup, raw_html, effective_url):
        meta = {
            name: self._meta_content(soup, name)
            for name in (
                "ColId",
                "SiteName",
                "SiteDomain",
                "SiteIDCode",
                "ColumnName",
                "ColumnType",
                "ArticleTitle",
                "PubDate",
                "ContentSource",
                "Keywords",
                "Author",
                "Description",
                "Url",
                "MakeTime",
            )
        }
        info = self._parse_info_table(soup)

        page_id = record.get("page_id") or self._page_id_from_url(effective_url)
        external_id = page_id or self._slug_from_url(effective_url)
        title = (
            self._clean_text(meta.get("ArticleTitle"))
            or info.get("标题")
            or record.get("title")
            or self._clean_text(soup.title.get_text(" ", strip=True) if soup.title else "")
        )
        listed_date = record.get("listed_date") or self._date_only(meta.get("PubDate"))
        listed_raw = record.get("listed_date_raw") or meta.get("PubDate") or ""
        published_date = (
            self._date_only(info.get("成文日期"))
            or self._date_only(meta.get("PubDate"))
            or listed_date
        )
        detail_posted_date = self._date_only(info.get("发布日期")) or self._date_only(meta.get("PubDate"))
        index_number = info.get("索引号") or ""
        post_number = self._post_number(index_number, external_id)
        department = info.get("所属机构") or ""
        publisher = (
            self._clean_text(meta.get("ContentSource"))
            or self._clean_text(meta.get("SiteName"))
            or "国家市场监督管理总局"
        )
        category = info.get("主题分类") or self.CATEGORY
        document_number = info.get("文号") or ""
        authors = self._clean_text(meta.get("Author"))
        keywords = self._keywords(meta.get("Keywords"))

        body_text = self._article_body_text(soup)
        description = self._clean_text(meta.get("Description"))
        abstract = body_text or description
        if body_text and description and description not in body_text and len(body_text) < self.TARGET_ABSTRACT_CHARS:
            abstract = self._join_text_parts([description, body_text])
        abstract = self._augment_short_abstract(
            abstract=abstract,
            title=title,
            category=category,
            publisher=publisher,
            department=department,
            published_date=published_date,
        )

        pdf_url = self._first_pdf_url(soup)
        original_filename = self._original_filename(pdf_url)
        image_urls = [
            self._absolute_url(img.get("src"))
            for img in soup.select(".Three_xilan_07 img[src]")
            if img.get("src")
        ]
        image_urls = [u for u in image_urls if u]

        related_html = self._fetch_article_unit_html(page_id, effective_url)
        related_links = self._parse_related_links(related_html, effective_url)
        if not pdf_url and related_html:
            related_soup = self._make_soup(related_html, context=f"article unit {page_id}")
            if related_soup is not None:
                pdf_url = self._first_pdf_url(related_soup)
                original_filename = self._original_filename(pdf_url)

        metadata = {
            "posted_date": listed_raw,
            "listed_date": listed_date,
            "detail_posted_date": detail_posted_date,
            "published_date_raw": info.get("成文日期") or meta.get("PubDate"),
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "page_id": page_id,
            "article_id": page_id,
            "article_slug": self._slug_from_url(effective_url),
            "column_page_id": self.COLUMN_PAGE_ID,
            "webId": self.WEB_ID,
            "tplSetId": self.TPL_SET_ID,
            "col_id": meta.get("ColId"),
            "site_id_code": meta.get("SiteIDCode"),
            "index_number": index_number,
            "post_number": post_number,
            "document_number": document_number,
            "column_name": meta.get("ColumnName"),
            "column_type": meta.get("ColumnType"),
            "make_time": meta.get("MakeTime"),
            "meta_description": description,
            "raw_meta": meta,
            "info_table": info,
            "list_record": record.get("raw") or {},
            "list_page": record.get("list_page"),
            "image_urls": image_urls,
            "related_links": related_links,
            "detail_api_endpoint": self.LIST_API,
            "detail_unit_tag": "文章正文2",
        }

        return {
            "id": external_id,
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": authors,
            "publisher": publisher,
            "department": department,
            "journal": None,
            "url": effective_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": None,
            "original_filename": original_filename,
            "metadata": metadata,
        }

    def _to_paper(self, parsed):
        metadata = dict(parsed.get("metadata") or {})
        metadata.update(
            {
                "post_number": parsed.get("post_number"),
                "listed_date": parsed.get("listed_date"),
                "posted_date": metadata.get("posted_date") or parsed.get("listed_date"),
                "original_filename": parsed.get("original_filename"),
            }
        )
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

    def _parse_info_table(self, soup):
        info = {}
        for ul in soup.select(".Three_xilan01_01 ul"):
            cells = [self._clean_text(li.get_text("", strip=True)) for li in ul.find_all("li", recursive=False)]
            cells = [cell for cell in cells if cell]
            if len(cells) < 2:
                continue
            label = cells[0].rstrip("：:")
            value = cells[1]
            if label:
                info[label] = value
        return info

    def _article_body_text(self, soup):
        body = soup.select_one(".Three_xilan_07")
        if body is None:
            return ""
        for tag in body.select("script, style, noscript"):
            tag.decompose()

        parts = []
        block_tags = body.find_all(["p", "div", "li", "td"], recursive=True)
        for tag in block_tags:
            text = self._clean_text(tag.get_text(" ", strip=True))
            if text and text not in parts:
                parts.append(text)
        if not parts:
            text = self._clean_text(body.get_text(" ", strip=True))
            if text:
                parts.append(text)
        return "\n".join(parts)

    def _parse_related_links(self, html, referer):
        if not html:
            return []
        soup = self._make_soup(html, context="related article links")
        if soup is None:
            return []
        links = []
        for a_tag in soup.select("a[href]"):
            href = self._absolute_url(a_tag.get("href"))
            title = self._clean_text(a_tag.get("title") or a_tag.get_text(" ", strip=True))
            if href:
                links.append({"title": title, "url": href})
        return links

    def _first_pdf_url(self, soup):
        for a_tag in soup.select("a[href]"):
            href = a_tag.get("href") or ""
            if ".pdf" in href.lower():
                return self._absolute_url(href)
        return None

    def _augment_short_abstract(self, abstract, title, category, publisher, department, published_date):
        abstract = self._clean_text(abstract)
        if len(abstract) >= self.TARGET_ABSTRACT_CHARS or len(abstract) < self.MIN_ABSTRACT_CHARS:
            return abstract
        supplement = self._join_text_parts(
            [
                f"标题：{title}" if title else "",
                f"分类：{category}" if category else "",
                f"发布机构：{publisher}" if publisher else "",
                f"所属机构：{department}" if department else "",
                f"发布日期：{published_date}" if published_date else "",
            ]
        )
        return self._join_text_parts([abstract, supplement])

    @staticmethod
    def _join_text_parts(parts):
        cleaned = []
        for part in parts:
            part = SamrGovCnZwCrawler._clean_text(part)
            if part and part not in cleaned:
                cleaned.append(part)
        return "\n".join(cleaned)

    @staticmethod
    def _clean_text(value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\xa0", " ").replace("\u3000", " ")
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _date_only(value):
        value = SamrGovCnZwCrawler._clean_text(value)
        if not value:
            return ""
        match = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", value)
        if match:
            year, month, day = match.groups()
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        match = re.search(r"(\d{4})(\d{2})(\d{2})", value)
        if match:
            year, month, day = match.groups()
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        return ""

    def _absolute_url(self, href):
        href = self._clean_text(href)
        if not href or href.lower().startswith(("javascript:", "mailto:")):
            return ""
        return urljoin(self.base_url + "/", href)

    @staticmethod
    def _meta_content(soup, name):
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content") is not None:
            return SamrGovCnZwCrawler._clean_text(tag.get("content"))
        return ""

    @staticmethod
    def _page_id_from_url(url):
        slug = SamrGovCnZwCrawler._slug_from_url(url)
        match = re.search(r"art_([A-Za-z0-9]+)", slug)
        if match:
            return match.group(1)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        for key in ("pageId", "articleId", "id"):
            if query.get(key):
                return SamrGovCnZwCrawler._clean_text(query[key][0])
        return slug

    @staticmethod
    def _slug_from_url(url):
        path = urlparse(url).path
        tail = path.rstrip("/").rsplit("/", 1)[-1]
        if tail.endswith(".html"):
            tail = tail[:-5]
        return tail

    @staticmethod
    def _post_number(index_number, fallback):
        index_number = SamrGovCnZwCrawler._clean_text(index_number)
        match = re.search(r"/\d{4}-(\d+)", index_number)
        if match:
            return match.group(1)
        match = re.search(r"(\d{5,})", index_number)
        if match:
            return match.group(1)
        return fallback or None

    @staticmethod
    def _keywords(value):
        value = SamrGovCnZwCrawler._clean_text(value)
        if not value:
            return None
        parts = re.split(r"[,，;；、]+", value)
        parts = [p.strip() for p in parts if p.strip()]
        return ", ".join(parts) if parts else None

    @staticmethod
    def _original_filename(pdf_url):
        if not pdf_url:
            return None
        tail = urlparse(pdf_url).path.rstrip("/").rsplit("/", 1)[-1]
        tail = unquote(tail)
        return tail or None

    def _budget_nearly_exhausted(self, started_at):
        return time.time() - started_at >= self.WALL_CLOCK_BUDGET_SECONDS - 30
