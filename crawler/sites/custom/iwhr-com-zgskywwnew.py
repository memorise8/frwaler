# -*- coding: utf-8 -*-
"""Crawler for IWHR Chinese site, "水科之声" issue archive.

Discovered endpoints:
  Start/list: http://www.iwhr.com/zgskywwnew/ddjs/newskzs/N014809index_1.htm
  Year list : /zgskywwnew/ddjs/newskzs/qk/{year}/N01480903NNindex_1.htm
  Detail    : /zgskywwnew/ddjs/newskzs/qk/{year}/webinfo/{yyyy}/{mm}/{id}.htm

The site is a static CMS. Recent detail pages are image-based issue pages, so
abstracts are built from real detail metadata plus embedded page-image facts.
"""

import json
import os
import re
import subprocess
import time
import uuid
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

from crawler.base_crawler import BaseCrawler


_SITE_ID = "iwhr-com-zgskywwnew"
_BASE_URL = "http://www.iwhr.com"
_START_URL = _BASE_URL + "/zgskywwnew/ddjs/newskzs/N014809index_1.htm"
_CATEGORY = "党的建设 / 水科之声 / 期刊"
_JOURNAL = "水科之声"
_PUBLISHER = "中国水科院党委; 中国水利水电科学研究院"
_DEPARTMENT = "中国水科院党委办公室"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))


def _make_soup(raw):
    """BeautifulSoup parser fallback: html5lib -> lxml -> html.parser."""
    if raw is None:
        return None
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None

    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception as exc:
            print(f"[{_SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
    return None


def _clean_text(value):
    if value is None:
        return ""
    text = unescape(str(value))
    text = text.replace("\xa0", " ").replace("\u3000", " ")
    return re.sub(r"\s+", " ", text).strip()


def _iso_date(value):
    text = _clean_text(value)
    if not text:
        return None
    m = re.search(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})", text)
    if not m:
        return None
    year, month, day = m.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def _filename_from_url(url):
    if not url:
        return None
    parsed = urlparse(url)
    tail = unquote(parsed.path.rstrip("/").split("/")[-1])
    return tail if tail else None


def _page_url(root_url, page):
    if page <= 1:
        return root_url
    if "index_1.htm" in root_url:
        return root_url.replace("index_1.htm", f"index_{page}.htm")
    return root_url


def _node_id_from_url(url):
    m = re.search(r"/(N\d+)index_\d+\.htm", url or "")
    return m.group(1) if m else None


def _native_slug(url):
    path_tail = urlparse(url).path.rstrip("/").split("/")[-1]
    if "." in path_tail:
        path_tail = path_tail.rsplit(".", 1)[0]
    return path_tail or None


def _post_number_from_slug(slug):
    if not slug:
        return None
    if slug.isdigit():
        return slug
    nums = re.findall(r"\d+", slug)
    return nums[-1] if nums else slug


def _issue_parts(title, issue_text):
    joined = _clean_text(f"{title or ''} {issue_text or ''}")
    year = None
    issue = None
    series = None
    m_year = re.search(r"(\d{4})年", joined)
    if m_year:
        year = m_year.group(1)
    m_issue = re.search(r"第\s*(\d+)\s*期", joined)
    if m_issue:
        issue = m_issue.group(1)
    m_series = re.search(r"(?:总第|总\s*第)\s*(\d+)\s*期", joined)
    if m_series:
        series = f"总第{m_series.group(1)}期"
    return year, issue, series


class IwhrComZgskywwnewCrawler(BaseCrawler):
    """Crawler for IWHR "水科之声" archive."""

    site_id = "iwhr-com-zgskywwnew"
    site_name = "Custom: iwhr-com-zgskywwnew"
    base_url = "http://www.iwhr.com"

    _START_URL = _START_URL

    def _curl_text(self, url, context="url", retries=3, timeout=45):
        """Fetch text via curl with retries and replacement decoding."""
        waits = [1, 3, 9]
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--max-time",
            "30",
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "-H",
            "Accept-Language: zh-CN,zh;q=0.9,en;q=0.6",
            url,
        ]
        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=timeout)
                if result.returncode == 0 and result.stdout:
                    return result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace")[:300]
                print(
                    f"[{self.site_id}] curl {context} attempt {attempt + 1}/{retries} "
                    f"failed rc={result.returncode}: {url} {stderr}"
                )
            except Exception as exc:
                print(
                    f"[{self.site_id}] curl {context} attempt {attempt + 1}/{retries} "
                    f"failed: {exc}"
                )
            if attempt < retries - 1:
                time.sleep(waits[attempt])
        print(f"[{self.site_id}] curl {context} failed after {retries} attempts: {url}")
        return None

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        list_pages_seen = 0
        start_time = time.time()
        limit_display = str(limit) if limit is not None else "inf"

        start_html = self._curl_text(self._START_URL, context="start page")
        if not start_html:
            print(f"[{self.site_id}] no start page, stopping")
            return 0

        start_soup = _make_soup(start_html)
        if start_soup is None:
            print(f"[{self.site_id}] start page parse failed, stopping")
            return 0

        roots = self._discover_list_roots(start_soup)

        for root_url, cached_html in roots:
            page = 1
            while True:
                if time.time() - start_time > _WALL_SECONDS:
                    print(f"[{self.site_id}] approaching 25-minute wall-clock budget, stopping")
                    return saved
                if limit is not None and saved >= limit:
                    return saved
                if list_pages_seen >= _MAX_PAGES:
                    print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached")
                    return saved

                list_url = _page_url(root_url, page)
                html = cached_html if page == 1 and cached_html is not None else None
                if html is None:
                    html = self._curl_text(list_url, context=f"list page {page}")
                list_pages_seen += 1

                if list_pages_seen % 10 == 0:
                    print(f"[{self.site_id}] page {list_pages_seen}: saved {saved}/{limit_display}")

                if not html:
                    print(f"[{self.site_id}] empty list response: {list_url}")
                    break

                soup = _make_soup(html)
                if soup is None:
                    print(f"[{self.site_id}] list parse failed: {list_url}")
                    break

                items = self._parse_list_items(soup, list_url)
                if not items:
                    print(f"[{self.site_id}] no records on list page: {list_url}")
                    break

                new_on_page = 0
                for item_index, item in enumerate(items, start=1):
                    if time.time() - start_time > _WALL_SECONDS:
                        print(f"[{self.site_id}] approaching 25-minute wall-clock budget, stopping")
                        return saved
                    if limit is not None and saved >= limit:
                        return saved

                    detail_url = item.get("url")
                    if not detail_url:
                        continue
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_on_page += 1

                    try:
                        paper = self._paper_from_item(item)
                        if paper is None:
                            pass
                        else:
                            abstract = paper.get("abstract") or ""
                            if len(abstract) < 50:
                                print(
                                    f"[{self.site_id}] skip item {item_index} "
                                    f"(abstract {len(abstract)} chars): {detail_url}"
                                )
                            else:
                                self._save_paper(paper)
                                saved += 1
                    except Exception as exc:
                        print(f"[{self.site_id}] item {detail_url} failed: {exc}")

                    time.sleep(max(float(getattr(self, "_delay", 1.0) or 0.0), 0.0))

                if new_on_page == 0:
                    print(f"[{self.site_id}] no new records on list page: {list_url}")
                    break

                if not self._has_next_page(soup, page):
                    break
                page += 1

        print(f"[{self.site_id}] done: saved {saved} total")
        return saved

    def _discover_list_roots(self, start_soup):
        roots = [(self._START_URL, start_soup.decode(formatter=None))]
        found = []
        for link in start_soup.select(".year a[href]"):
            href = link.get("href")
            url = urljoin(self.base_url, href)
            if "N01480903" not in url or "index_1.htm" not in url:
                continue
            text = _clean_text(link.get_text(" ", strip=True))
            m = re.search(r"(\d{4})", text + " " + url)
            year = int(m.group(1)) if m else 0
            found.append((year, url))

        seen = {self._START_URL}
        for _year, url in sorted(found, key=lambda pair: pair[0], reverse=True):
            if url not in seen:
                roots.append((url, None))
                seen.add(url)
        return roots

    def _parse_list_items(self, soup, list_url):
        container = soup.select_one(".skzs")
        if container is None:
            return []

        items = []
        for li in container.find_all("li"):
            link = li.find("a", href=True)
            if link is None:
                continue
            href = link.get("href")
            if not href or href.startswith("#"):
                continue

            detail_url = urljoin(self.base_url, href)
            spans = [_clean_text(span.get_text(" ", strip=True)) for span in li.find_all("span")]
            spans = [text for text in spans if text]
            title = spans[0] if spans else _clean_text(link.get_text(" ", strip=True))
            img = li.find("img")
            cover_url = urljoin(self.base_url, img.get("src")) if img and img.get("src") else None
            issue_text = _clean_text(" ".join(spans[1:]))
            year, issue, series = _issue_parts(title, issue_text)

            items.append(
                {
                    "url": detail_url,
                    "title": title,
                    "issue_text": issue_text,
                    "cover_image_url": cover_url,
                    "list_url": list_url,
                    "list_node_id": _node_id_from_url(list_url),
                    "year": year,
                    "issue": issue,
                    "series": series,
                }
            )
        return items

    def _has_next_page(self, soup, page):
        next_num = f"index_{page + 1}.htm"
        if soup.find("a", href=lambda href: href and next_num in str(href)):
            return True
        next_words = ("下一页", "下页", "Next", "next", ">")
        for link in soup.find_all("a", href=True):
            text = _clean_text(link.get_text(" ", strip=True))
            if any(word in text for word in next_words):
                return True
        return False

    def _paper_from_item(self, item):
        url = item["url"]
        if url.lower().split("?", 1)[0].endswith(".pdf"):
            return self._paper_from_direct_file(item)

        html = self._curl_text(url, context="detail page")
        if not html:
            print(f"[{self.site_id}] skip detail with no response: {url}")
            return None

        soup = _make_soup(html)
        if soup is None:
            print(f"[{self.site_id}] skip detail parse failure: {url}")
            return None

        return self._paper_from_detail_html(item, soup, html)

    def _paper_from_direct_file(self, item):
        url = item["url"]
        slug = _native_slug(url)
        external_id = slug or item.get("title") or url
        post_number = _post_number_from_slug(slug) or external_id
        filename = _filename_from_url(url)
        listed_date = self._date_from_item(item)
        title = self._title_with_series(item)
        abstract = self._abstract_from_metadata(
            title=title,
            posted_date=listed_date,
            source="",
            item=item,
            body_text="",
            body_image_urls=[],
            pdf_url=url,
        )
        metadata = self._metadata(
            item=item,
            url=url,
            posted_date=listed_date,
            posted_date_raw=listed_date,
            source="",
            external_id=external_id,
            post_number=post_number,
            original_filename=filename,
            content_type="direct_pdf",
            body_image_urls=[],
            cms_publishdate=None,
        )
        return self._paper_dict(
            url=url,
            title=title,
            abstract=abstract,
            external_id=external_id,
            post_number=post_number,
            published_date=listed_date,
            listed_date=listed_date,
            pdf_url=url,
            original_filename=filename,
            metadata=metadata,
        )

    def _paper_from_detail_html(self, item, soup, html):
        url = item["url"]
        h1 = soup.find("h1")
        title = _clean_text(h1.get_text(" ", strip=True)) if h1 else ""
        title = title or self._title_with_series(item)

        other_text = ""
        other = soup.select_one(".other")
        if other is not None:
            other_text = _clean_text(other.get_text(" ", strip=True))
        date_match = re.search(r"发布时间[:：]?\s*(\d{4}[-/年.]\d{1,2}[-/月.]\d{1,2})", other_text)
        posted_raw = date_match.group(1) if date_match else None
        posted_date = _iso_date(posted_raw)
        source_match = re.search(r"来源[:：]?\s*([^【]+)", other_text)
        source = _clean_text(source_match.group(1)) if source_match else ""

        body = soup.select_one("#BodyLabel") or soup.select_one(".nr") or soup.select_one(".zwcontent")
        body_text = ""
        body_image_urls = []
        pdf_url = None
        original_filename = None
        if body is not None:
            body_text = _clean_text(body.get_text(" ", strip=True))
            for img in body.find_all("img"):
                src = img.get("src")
                if src:
                    body_image_urls.append(urljoin(self.base_url, src))
            for link in body.find_all("a", href=True):
                href = link.get("href")
                abs_href = urljoin(self.base_url, href)
                if abs_href.lower().split("?", 1)[0].endswith(".pdf"):
                    pdf_url = abs_href
                    original_filename = _filename_from_url(abs_href)
                    break

        slug = _native_slug(url)
        external_id = slug or item.get("title") or url
        post_number = _post_number_from_slug(slug) or external_id
        listed_date = posted_date or self._date_from_item(item)
        cms_publishdate = self._cms_publishdate(html)
        if posted_date is None:
            posted_date = _iso_date(cms_publishdate) or listed_date

        abstract = self._abstract_from_metadata(
            title=title,
            posted_date=posted_date,
            source=source,
            item=item,
            body_text=body_text,
            body_image_urls=body_image_urls,
            pdf_url=pdf_url,
        )

        metadata = self._metadata(
            item=item,
            url=url,
            posted_date=listed_date,
            posted_date_raw=posted_raw,
            source=source,
            external_id=external_id,
            post_number=post_number,
            original_filename=original_filename,
            content_type="html_detail",
            body_image_urls=body_image_urls,
            cms_publishdate=cms_publishdate,
        )
        return self._paper_dict(
            url=url,
            title=title,
            abstract=abstract,
            external_id=external_id,
            post_number=post_number,
            published_date=posted_date,
            listed_date=listed_date,
            pdf_url=pdf_url,
            original_filename=original_filename,
            metadata=metadata,
        )

    def _title_with_series(self, item):
        title = _clean_text(item.get("title"))
        issue_text = _clean_text(item.get("issue_text"))
        if issue_text and issue_text not in title:
            return f"{title} {issue_text}".strip()
        return title

    def _date_from_item(self, item):
        year = item.get("year")
        if year and re.fullmatch(r"\d{4}", str(year)):
            return f"{int(year):04d}-01-01"
        return None

    def _cms_publishdate(self, html):
        m = re.search(r"publishdate\s*:\s*(\d{4}/\d{1,2}/\d{1,2}(?:\s+\d{1,2}:\d{2}:\d{2})?)", html)
        return m.group(1) if m else None

    def _abstract_from_metadata(self, title, posted_date, source, item, body_text, body_image_urls, pdf_url):
        parts = []
        usable_body = body_text
        if usable_body:
            usable_body = re.sub(r"网站群管理", " ", usable_body)
            usable_body = _clean_text(usable_body)
            if len(usable_body) >= 50:
                parts.append(usable_body)

        title_piece = f"《{_JOURNAL}》{title}"
        meta_bits = []
        if item.get("series"):
            meta_bits.append(str(item["series"]))
        if posted_date:
            meta_bits.append(f"发布时间：{posted_date}")
        if source:
            meta_bits.append(f"来源：{source}")
        meta_bits.append(f"栏目：{_CATEGORY}")
        meta_bits.append(f"主办：{_PUBLISHER}")
        if body_image_urls:
            meta_bits.append(f"正文为图片版期刊，共{len(body_image_urls)}个内嵌版面图片")
            meta_bits.append(f"首个版面图片：{body_image_urls[0]}")
        if pdf_url:
            meta_bits.append(f"PDF原文：{pdf_url}")
        if item.get("cover_image_url"):
            meta_bits.append(f"列表封面：{item['cover_image_url']}")
        parts.append(title_piece + "，" + "；".join(meta_bits) + "。")
        return _clean_text(" ".join(parts))

    def _metadata(
        self,
        item,
        url,
        posted_date,
        posted_date_raw,
        source,
        external_id,
        post_number,
        original_filename,
        content_type,
        body_image_urls,
        cms_publishdate,
    ):
        meta = {
            "posted_date": posted_date,
            "posted_date_raw": posted_date_raw,
            "listed_date": posted_date,
            "originalFilename": original_filename,
            "journal_raw": _JOURNAL,
            "series": item.get("series"),
            "volume": item.get("year"),
            "issue": item.get("issue"),
            "node_id": item.get("list_node_id"),
            "native_slug": external_id,
            "post_number": post_number,
            "source": source,
            "category": _CATEGORY,
            "list_url": item.get("list_url"),
            "detail_url": url,
            "cover_image_url": item.get("cover_image_url"),
            "body_image_count": len(body_image_urls),
            "body_image_urls": body_image_urls,
            "cms_publishdate": cms_publishdate,
            "content_type": content_type,
        }
        return json.dumps(meta, ensure_ascii=False)

    def _paper_dict(
        self,
        url,
        title,
        abstract,
        external_id,
        post_number,
        published_date,
        listed_date,
        pdf_url,
        original_filename,
        metadata,
    ):
        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, url)),
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": None,
            "publisher": _PUBLISHER,
            "department": _DEPARTMENT,
            "journal": _JOURNAL,
            "url": url,
            "pdf_url": pdf_url,
            "keywords": None,
            "category": _CATEGORY,
            "doi": None,
            "original_filename": original_filename,
            "metadata": metadata,
        }
