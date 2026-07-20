# -*- coding: utf-8 -*-
"""Crawler for MOT China government information disclosure statistics pages."""

from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class XxgkMotGovCn2020Crawler(BaseCrawler):
    site_id = "xxgk-mot-gov-cn-2020"
    site_name = "Custom: xxgk-mot-gov-cn-2020"
    base_url = "https://xxgk.mot.gov.cn"

    _START_URL = (
        "https://xxgk.mot.gov.cn/2020/zhengce/863/868/869/"
        "iframe_list_7232.html"
    )
    _LIST_DIR = "https://xxgk.mot.gov.cn/2020/zhengce/863/868/869/"
    _LIST_STEM = "iframe_list_7232"
    _COLUMN_ID = "7232"
    _DEFAULT_CATEGORY = "统计数据"
    _MAX_PAGES = 200
    _MAX_SECONDS = 25 * 60
    _MIN_ABSTRACT_CHARS = 50

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay

    # ------------------------------------------------------------------
    # Network / parsing helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, referer=None, timeout=45):
        """Fetch text with curl, retrying failures with 1s/3s/9s backoff."""
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
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,"
            "application/json,*/*;q=0.8",
            "-H",
            "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
            "-w",
            "\n__HTTP_STATUS__:%{http_code}",
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
                stdout = result.stdout or b""
                body, status = self._split_curl_status(stdout)
                if result.returncode == 0 and status and 200 <= status < 400 and body:
                    return self._decode_bytes(body)
                stderr = self._decode_bytes(result.stderr or b"").strip()
                last_error = stderr or f"curl exit {result.returncode}, status {status}"
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
    def _split_curl_status(raw):
        marker = b"\n__HTTP_STATUS__:"
        if marker not in raw:
            return raw, None
        body, status_raw = raw.rsplit(marker, 1)
        try:
            return body, int(status_raw.strip()[:3])
        except (TypeError, ValueError):
            return body, None

    @staticmethod
    def _decode_bytes(raw):
        if raw is None:
            return ""
        for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk", "gb2312"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    def _make_soup(self, raw):
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
        value = re.sub(r"<br\s*/?>", " ", value, flags=re.I)
        value = re.sub(r"<[^>]+>", " ", value)
        value = value.replace("\xa0", " ").replace("\u3000", " ")
        value = value.replace("　", " ")
        value = re.sub(r"\s+", " ", value)
        return value.strip()

    @classmethod
    def _text_from_node(cls, node, separator=" "):
        if node is None:
            return ""
        return cls._clean_text(node.get_text(separator, strip=True))

    @classmethod
    def _date_only(cls, value):
        value = cls._clean_text(value)
        if not value:
            return None
        match = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", value)
        if match:
            year, month, day = match.groups()
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        match = re.search(r"(\d{4})(\d{2})(\d{2})", value)
        if match:
            year, month, day = match.groups()
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        return None

    @staticmethod
    def _join_values(values, sep):
        cleaned = []
        seen = set()
        for value in values:
            text = XxgkMotGovCn2020Crawler._clean_text(value)
            if text and text not in seen:
                cleaned.append(text)
                seen.add(text)
        return sep.join(cleaned) if cleaned else None

    def _meta(self, soup, name):
        name_l = name.lower()
        tag = soup.find(
            lambda t: (
                getattr(t, "name", None) == "meta"
                and self._clean_text(t.get("name")).lower() == name_l
            )
        )
        if tag and tag.get("content") is not None:
            return self._clean_text(tag.get("content"))
        return ""

    def _absolute_url(self, href, base):
        href = self._clean_text(href)
        if not href or href.lower().startswith(("javascript:", "mailto:")):
            return None
        return urljoin(base, href)

    def _list_url(self, page):
        if page <= 1:
            return self._START_URL
        return f"{self._LIST_DIR}{self._LIST_STEM}_{page - 1}.html"

    @staticmethod
    def _filename_from_url(url):
        if not url:
            return None
        path = unquote(urlparse(url).path or "")
        tail = path.rstrip("/").split("/")[-1]
        return tail or None

    @staticmethod
    def _external_id_from_url(url):
        match = re.search(r"/t\d+_(\d+)\.html(?:$|[?#])", url or "")
        if match:
            return match.group(1)
        tail = (urlparse(url).path or "").rstrip("/").split("/")[-1]
        return tail or (url or None)

    @staticmethod
    def _node_id_from_url(url):
        match = re.search(r"/jigou/([^/]+)/", url or "")
        return match.group(1) if match else None

    # ------------------------------------------------------------------
    # List/detail parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, raw, page, page_url):
        soup = self._make_soup(raw)
        total_pages = None
        current_page_index = None
        match = re.search(
            r"createPageHTML\(\s*(\d+)\s*,\s*(\d+)\s*,\s*"
            r"['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]",
            raw or "",
        )
        if match:
            total_pages = int(match.group(1))
            current_page_index = int(match.group(2))

        category = self._DEFAULT_CATEGORY
        heading = soup.select_one(".national_development_title span")
        if heading:
            category = self._text_from_node(heading) or category

        rows = []
        list_items = soup.select("ul.national_development li")
        if not list_items:
            list_items = [
                li for li in soup.find_all("li")
                if li.find("a", href=re.compile(r"\.html(?:$|[?#])"))
            ]

        for li in list_items:
            link = li.find("a", href=True)
            if not link:
                continue
            url = self._absolute_url(link.get("href"), page_url)
            if not url:
                continue
            title = self._clean_text(link.get("title")) or self._text_from_node(link)
            if not title:
                continue
            span = li.find("span")
            listed_raw = self._text_from_node(span) if span else ""
            listed_date = self._date_only(listed_raw)
            external_id = self._external_id_from_url(url)
            rows.append({
                "title": title,
                "url": url,
                "listed_date": listed_date,
                "listed_date_raw": listed_raw,
                "external_id": external_id,
                "post_number": external_id if external_id and external_id.isdigit() else external_id,
                "category": category,
                "list_page": page,
                "list_page_index": current_page_index,
                "list_url": page_url,
                "href": link.get("href"),
            })

        return rows, total_pages

    def _extract_info_fields(self, soup):
        fields = {}
        for group in soup.select(".form-group"):
            label = group.find("label")
            if not label:
                continue
            key = self._text_from_node(label).rstrip("：:")
            if not key or key in fields:
                continue
            value_node = group.select_one(".form-control-static")
            if not value_node:
                paragraphs = group.find_all("p")
                value_node = paragraphs[-1] if paragraphs else None
            value = self._text_from_node(value_node)
            if value:
                fields[key] = value
        return fields

    def _extract_body(self, soup, detail_url):
        zoom = soup.select_one("#Zoom")
        if not zoom:
            return "", [], ""

        body_soup = BeautifulSoup(str(zoom), "html.parser")
        for tag in body_soup(["script", "style"]):
            tag.decompose()

        images = []
        for img in body_soup.find_all("img"):
            src = self._absolute_url(img.get("src"), detail_url)
            image = {
                "src": src,
                "alt": self._clean_text(img.get("alt")),
                "title": self._clean_text(img.get("title")),
                "picname": self._clean_text(img.get("picname")),
                "oldsrc": self._clean_text(img.get("oldsrc") or img.get("OLDSRC")),
            }
            image = {k: v for k, v in image.items() if v}
            if image:
                images.append(image)

        text = self._text_from_node(body_soup, separator="\n")
        if images:
            image_names = [
                img.get("picname") or img.get("alt") or img.get("title")
                or self._filename_from_url(img.get("src"))
                for img in images
            ]
            image_text = self._join_values(image_names, "; ")
            if image_text and image_text not in text:
                text = self._join_values([text, f"正文图像: {image_text}"], "\n")
        return text or "", images, str(zoom)

    def _extract_attachments(self, soup, detail_url):
        attachments = []
        seen = set()
        for container in soup.select(".gksqxz_fj"):
            for link in container.find_all("a", href=True):
                url = self._absolute_url(link.get("href"), detail_url)
                if not url or url in seen:
                    continue
                seen.add(url)
                filename = (
                    self._clean_text(link.get("download"))
                    or self._text_from_node(link)
                    or self._filename_from_url(url)
                )
                attachments.append({
                    "url": url,
                    "filename": filename,
                    "extension": (
                        "." + self._filename_from_url(url).split(".")[-1].lower()
                        if self._filename_from_url(url) and "." in self._filename_from_url(url)
                        else None
                    ),
                })
        return attachments

    def _parse_detail_page(self, raw, item):
        soup = self._make_soup(raw)
        detail_url = item["url"]

        info_fields = self._extract_info_fields(soup)
        body_text, images, body_html = self._extract_body(soup, detail_url)
        attachments = self._extract_attachments(soup, detail_url)

        title = (
            self._meta(soup, "ArticleTitle")
            or self._text_from_node(soup.select_one("h1.gkzn_right_tit span"))
            or item["title"]
        )
        pub_date_raw = self._meta(soup, "PubDate")
        published_date = (
            self._date_only(pub_date_raw)
            or self._date_only(info_fields.get("公开日期"))
            or item.get("listed_date")
        )
        listed_date = item.get("listed_date") or self._date_only(info_fields.get("公开日期"))
        listed_raw = item.get("listed_date_raw") or info_fields.get("公开日期") or ""

        keywords_raw = (
            self._meta(soup, "Keywords")
            or info_fields.get("主题词")
            or ""
        )
        keyword_parts = [
            p.strip()
            for p in re.split(r"[,，;；、]+", keywords_raw)
            if p and p.strip()
        ]
        keywords = ", ".join(keyword_parts) if keyword_parts else None

        author = self._meta(soup, "Author")
        authors = self._join_values(re.split(r"[;,；、]+", author), "; ") if author else None

        site_name = self._meta(soup, "SiteName")
        content_source = self._meta(soup, "ContentSource")
        publisher = content_source or site_name or "交通运输部"
        department = info_fields.get("机构分类") or self._meta(soup, "ColumnName") or None
        category = info_fields.get("主题分类") or item.get("category") or self._meta(soup, "ColumnType")
        description = self._meta(soup, "Description")

        attachment_lines = [
            f"{att.get('filename')}: {att.get('url')}"
            for att in attachments
            if att.get("filename") or att.get("url")
        ]
        field_line = "；".join(
            f"{k}: {v}" for k, v in info_fields.items()
            if k in {"索引号", "文号", "公开日期", "主题词", "机构分类", "主题分类", "公文类型"}
        )
        abstract = self._join_values(
            [
                description,
                field_line,
                body_text,
                "附件下载: " + "；".join(attachment_lines) if attachment_lines else "",
            ],
            "\n",
        ) or ""

        pdf_attachment = next(
            (
                att for att in attachments
                if (att.get("url") or "").lower().split("?", 1)[0].endswith(".pdf")
            ),
            None,
        )
        pdf_url = pdf_attachment.get("url") if pdf_attachment else None
        original_filename = None
        if pdf_attachment:
            original_filename = pdf_attachment.get("filename") or self._filename_from_url(pdf_url)
        elif attachments:
            original_filename = attachments[0].get("filename") or self._filename_from_url(attachments[0].get("url"))

        external_id = item.get("external_id") or self._external_id_from_url(detail_url)
        post_number = item.get("post_number") or external_id
        metadata = {
            "posted_date": listed_raw,
            "listed_date": listed_date,
            "originalFilename": original_filename,
            "node_id": self._node_id_from_url(detail_url),
            "column_id": self._COLUMN_ID,
            "article_id": external_id,
            "post_number": post_number,
            "list_page": item.get("list_page"),
            "list_page_index": item.get("list_page_index"),
            "list_url": item.get("list_url"),
            "list_href": item.get("href"),
            "detail_url": detail_url,
            "pub_date_raw": pub_date_raw,
            "listed_date_raw": listed_raw,
            "site_name": site_name,
            "content_source": content_source,
            "column_name": self._meta(soup, "ColumnName"),
            "column_type": self._meta(soup, "ColumnType"),
            "index_number": info_fields.get("索引号"),
            "document_number": info_fields.get("文号"),
            "document_type": info_fields.get("公文类型"),
            "info_fields": info_fields,
            "attachments": attachments,
            "images": images,
            "body_html_present": bool(body_html),
            "description": description,
        }
        metadata = {k: v for k, v in metadata.items() if v not in (None, "", [], {})}

        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, detail_url)),
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
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        if limit is not None and limit <= 0:
            return 0

        saved = 0
        page = 1
        total_pages = None
        seen_urls = set()
        started_at = time.monotonic()
        limit_or_inf = limit if limit is not None else "inf"

        while page <= self._MAX_PAGES:
            if limit is not None and saved >= limit:
                break
            if time.monotonic() - started_at >= self._MAX_SECONDS - 30:
                print(f"[{self.site_id}] approaching 25 minute crawl budget; exiting cleanly")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            page_url = self._list_url(page)
            raw = self._curl_get(page_url, referer=self._START_URL)
            if not raw:
                print(f"[{self.site_id}] empty or failed list page {page}; stopping")
                break

            items, page_total = self._parse_list_page(raw, page, page_url)
            if page_total:
                total_pages = page_total

            if not items:
                print(f"[{self.site_id}] page {page} returned 0 records; stopping")
                break

            new_items = []
            for item in items:
                url = item.get("url")
                if not url:
                    continue
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_items.append(item)

            if not new_items:
                print(f"[{self.site_id}] page {page} had no new URLs; stopping")
                break

            for index, item in enumerate(new_items, start=1):
                if limit is not None and saved >= limit:
                    break
                if time.monotonic() - started_at >= self._MAX_SECONDS - 30:
                    print(f"[{self.site_id}] approaching 25 minute crawl budget; exiting cleanly")
                    return saved

                try:
                    time.sleep(self.detail_delay)
                    detail_raw = self._curl_get(item["url"], referer=page_url)
                    if not detail_raw:
                        print(f"[{self.site_id}] item {index} failed: empty detail response")
                        continue
                    paper = self._parse_detail_page(detail_raw, item)
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self._MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {index} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue
                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:80]}")
                except Exception as exc:
                    print(f"[{self.site_id}] item {index} failed: {exc}")
                    continue

            if total_pages is not None and page >= total_pages:
                break
            page += 1

        if page > self._MAX_PAGES:
            print(f"[{self.site_id}] reached safety page cap ({self._MAX_PAGES}); stopping")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
