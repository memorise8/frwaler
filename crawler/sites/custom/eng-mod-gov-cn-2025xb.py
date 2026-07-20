# -*- coding: utf-8 -*-
"""Crawler for eng.mod.gov.cn 2025 English Laws channel.

Discovery notes:
- Start URL: http://eng.mod.gov.cn/2025xb/M/L_251592/index.html
- The live channel is static HTML, not JSON. Pagination is driven by
  material/js/fenye.js with createPageHTML('1', '1', 'index', 'html', '10', ...).
- Detail pages are static HTML with native metadata such as contentid,
  catalogs, publishdate, author/editor, Source, and Time.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from urllib.parse import unquote, urljoin, urlparse

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BeautifulSoup
except ImportError:  # pragma: no cover - dependency is expected in this project
    _BeautifulSoup = None


SITE_ID = "eng-mod-gov-cn-2025xb"
BASE_URL = "http://eng.mod.gov.cn"
START_URL = BASE_URL + "/2025xb/M/L_251592/index.html"
MAX_PAGES = 200
MAX_SECONDS = 25 * 60
FETCH_MAX_TIME = 30
FETCH_TIMEOUT = 45
RETRY_WAITS = (1, 3, 9)


def _make_soup(raw):
    """Parse HTML with a strict fallback chain and never raise."""
    if raw is None or _BeautifulSoup is None:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return _BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _clean_text(value):
    if value is None:
        return ""
    text = str(value).replace("\xa0", " ")
    text = re.sub(r"[\u2000-\u200f\u2028-\u202f\u205f\u3000]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _iso_date(value):
    if not value:
        return None
    match = re.search(r"(20\d{2}|19\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", str(value))
    if not match:
        return None
    year, month, day = match.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}"


def _first_number(value):
    if not value:
        return None
    match = re.search(r"(\d{4,})", str(value))
    return match.group(1) if match else None


def _meta_content(soup, key):
    """Return a meta content by name/id, including the site's typo ame=publishdate."""
    if soup is None:
        return None
    wanted = key.lower()
    for tag in soup.find_all("meta"):
        names = [
            tag.get("name"),
            tag.get("id"),
            tag.get("property"),
            tag.get("ame"),
        ]
        if any(str(name).strip().lower() == wanted for name in names if name):
            content = tag.get("content")
            return _clean_text(content) if content is not None else None
    return None


def _filename_from_url(url):
    if not url:
        return None
    path = urlparse(url).path
    tail = unquote(path.rstrip("/").split("/")[-1])
    if "." in tail and len(tail) <= 220:
        return tail
    return None


def _filename_from_content_disposition(value):
    if not value:
        return None
    match = re.search(r"filename\*\s*=\s*[^']*''([^;\r\n]+)", value, re.I)
    if match:
        return unquote(match.group(1).strip().strip('"'))
    match = re.search(r"filename\s*=\s*\"?([^\";\r\n]+)\"?", value, re.I)
    if match:
        return unquote(match.group(1).strip())
    return None


def _json_dumps(data):
    clean = {k: v for k, v in data.items() if v not in (None, "", [], {})}
    return json.dumps(clean, ensure_ascii=False, sort_keys=True) if clean else None


class EngModGovCn2025xbCrawler(BaseCrawler):
    site_id = SITE_ID
    site_name = "Custom: eng-mod-gov-cn-2025xb"
    base_url = BASE_URL

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls = set()
        limit_value = limit if limit is not None else float("inf")
        limit_or_inf = str(limit) if limit is not None else "inf"
        start_time = time.time()

        while saved < limit_value and page <= MAX_PAGES:
            if self._approaching_budget(start_time):
                print(f"[{self.site_id}] wall-clock budget approaching, stopping cleanly")
                break

            list_url = self._list_url(page)
            html = self._curl_get(list_url, context=f"page {page}")
            if not html:
                print(f"[{self.site_id}] page {page}: fetch failed or empty, stopping")
                break

            soup = _make_soup(html)
            if soup is None:
                print(f"[{self.site_id}] page {page}: parse failed, stopping")
                break

            page_info = self._parse_page_info(html)
            items = self._parse_list_items(soup, page)
            if not items:
                print(f"[{self.site_id}] page {page}: 0 list records, stopping")
                break

            new_urls_on_page = 0
            for item_index, item in enumerate(items, start=1):
                if saved >= limit_value:
                    break
                if self._approaching_budget(start_time):
                    print(f"[{self.site_id}] wall-clock budget approaching during page {page}, stopping cleanly")
                    return saved

                detail_url = item.get("url")
                if not detail_url:
                    continue
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_urls_on_page += 1

                item_label = detail_url
                try:
                    paper = self._parse_detail(item, page_info, item_index)
                    if paper is None:
                        continue
                    self._save_paper(paper)
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue
                finally:
                    time.sleep(self._detail_delay())

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            if new_urls_on_page == 0:
                print(f"[{self.site_id}] page {page}: 0 new URLs, stopping")
                break

            page_count = page_info.get("page_count")
            if page_count is not None and page >= page_count:
                break
            if not self._has_next_page(soup, page, page_count):
                break

            page += 1

        if page > MAX_PAGES:
            print(f"[{self.site_id}] safety cap of {MAX_PAGES} pages reached")

        print(f"[{self.site_id}] crawl complete: saved {saved} records")
        return saved

    def _detail_delay(self):
        delay = getattr(self, "_delay", None)
        return 1.0 if delay is None else float(delay)

    def _approaching_budget(self, start_time):
        return (time.time() - start_time) >= (MAX_SECONDS - FETCH_TIMEOUT)

    def _list_url(self, page):
        if page <= 1:
            return START_URL
        return urljoin(START_URL, f"index_{page}.html")

    def _curl_get(self, url, context="request"):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-sk",
            "-L",
            "--max-time",
            str(FETCH_MAX_TIME),
            "-A",
            self.USER_AGENT,
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "-w",
            "\n__HTTP_CODE__:%{http_code}",
            url,
        ]

        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=FETCH_TIMEOUT)
                text = result.stdout.decode("utf-8", errors="replace")
                body, status = self._split_curl_status(text)

                if result.returncode != 0:
                    raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip() or f"curl rc={result.returncode}")
                if status is not None and 400 <= status < 500:
                    print(f"[{self.site_id}] {context}: HTTP {status} for {url}")
                    return None
                if status is not None and status >= 500:
                    raise RuntimeError(f"HTTP {status}")
                if body and body.strip():
                    return body
                raise RuntimeError("empty response")
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                if attempt < 2:
                    wait = RETRY_WAITS[attempt]
                    print(f"[{self.site_id}] {context}: fetch attempt {attempt + 1}/3 failed for {url}: {exc}; retrying in {wait}s")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] {context}: fetch failed after 3 attempts for {url}: {exc}")
        return None

    def _curl_head_filename(self, url):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-sk",
            "-I",
            "-L",
            "--max-time",
            "15",
            "-A",
            self.USER_AGENT,
            url,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=20)
        except Exception:
            return None
        if result.returncode != 0:
            return None
        headers = result.stdout.decode("utf-8", errors="replace")
        for line in headers.splitlines():
            if line.lower().startswith("content-disposition:"):
                return _filename_from_content_disposition(line.split(":", 1)[1].strip())
        return None

    def _split_curl_status(self, text):
        marker = "\n__HTTP_CODE__:"
        if marker not in text:
            return text, None
        body, code_text = text.rsplit(marker, 1)
        try:
            return body, int(code_text.strip().splitlines()[0])
        except (ValueError, IndexError):
            return body, None

    def _parse_page_info(self, html):
        info = {
            "page_count": None,
            "current_page": None,
            "page_name": None,
            "page_ext": None,
            "record_count": None,
            "pagination_type": None,
        }
        match = re.search(
            r"createPageHTML\s*\(\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'",
            html,
            re.S,
        )
        if not match:
            return info

        page_count, current_page, page_name, page_ext, record_count, pagination_type = match.groups()
        info.update(
            {
                "page_count": self._to_int(page_count),
                "current_page": self._to_int(current_page),
                "page_name": page_name or None,
                "page_ext": page_ext or None,
                "record_count": self._to_int(record_count),
                "pagination_type": pagination_type or None,
            }
        )
        return info

    def _to_int(self, value):
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    def _parse_list_items(self, soup, page):
        items = []
        for li in soup.select("ul#main-news-list > li"):
            link = li.find("a", href=True)
            if not link:
                continue
            href = link.get("href")
            detail_url = urljoin(START_URL, href)
            title_node = li.select_one("h3.title") or link
            date_node = li.select_one("small.time")
            image_node = li.find("img", src=True)
            items.append(
                {
                    "url": detail_url,
                    "title": _clean_text(title_node.get_text(" ", strip=True)),
                    "listed_date_raw": _clean_text(date_node.get_text(" ", strip=True)) if date_node else None,
                    "image_url": urljoin(START_URL, image_node.get("src")) if image_node else None,
                    "page_number": page,
                }
            )
        return items

    def _has_next_page(self, soup, page, page_count):
        if page_count is not None:
            return page < page_count
        next_href = f"index_{page + 1}.html"
        for link in soup.find_all("a", href=True):
            text = _clean_text(link.get_text(" ", strip=True)).lower()
            href = link.get("href") or ""
            if next_href in href or text == "next":
                return True
        return False

    def _parse_detail(self, list_item, page_info, item_index):
        detail_url = list_item["url"]
        html = self._curl_get(detail_url, context=f"item {detail_url}")
        if not html:
            return None

        soup = _make_soup(html)
        if soup is None:
            print(f"[{self.site_id}] item {detail_url}: detail parse failed")
            return None

        title_node = soup.select_one("div.artichle-info h2") or soup.find("h1")
        title = _clean_text(title_node.get_text(" ", strip=True)) if title_node else list_item.get("title")
        if not title:
            print(f"[{self.site_id}] item {detail_url}: missing title, skipping")
            return None

        info = self._article_info(soup)
        body_node = (
            soup.select_one("#article-content .article-content #main-news-list")
            or soup.select_one("#article-content .article-content")
            or soup.select_one("#article-content")
        )
        if body_node is None:
            print(f"[{self.site_id}] item {detail_url}: missing article body, skipping")
            return None

        for tag in body_node.select("script, style, .more-page, #displaypagenum, #mediaurl, #cmplayer"):
            tag.decompose()
        abstract = _clean_text(body_node.get_text(" ", strip=True))
        if len(abstract) < 50:
            print(f"[{self.site_id}] item {detail_url}: abstract {len(abstract)} chars (<50), skipping")
            return None

        content_id_raw = _meta_content(soup, "contentid")
        catalogs_raw = _meta_content(soup, "catalogs")
        post_number = _first_number(content_id_raw) or _first_number(detail_url)
        external_id = post_number or content_id_raw or detail_url
        node_id = _first_number(catalogs_raw)

        published_raw = _meta_content(soup, "publishdate") or info.get("time")
        listed_raw = list_item.get("listed_date_raw") or published_raw
        published_date = _iso_date(published_raw) or _iso_date(listed_raw)
        listed_date = _iso_date(listed_raw) or published_date

        pdf_url = self._find_pdf_url(soup, detail_url)
        original_filename = _filename_from_url(pdf_url)
        if pdf_url and not original_filename:
            original_filename = self._curl_head_filename(pdf_url)

        keywords = _meta_content(soup, "keywords")
        author = _meta_content(soup, "author")
        reporter = _meta_content(soup, "reporter")
        editor = _meta_content(soup, "editor") or info.get("editor")
        source = info.get("source")

        metadata = {
            "posted_date": listed_raw,
            "originalFilename": original_filename,
            "content_id": content_id_raw,
            "content_id_number": post_number,
            "node_id": node_id,
            "catalogs": catalogs_raw,
            "source": source,
            "editor": editor,
            "author": author,
            "reporter": reporter,
            "time_raw": info.get("time"),
            "published_date_raw": published_raw,
            "listed_date": listed_date,
            "list_date_raw": list_item.get("listed_date_raw"),
            "list_title": list_item.get("title"),
            "image_url": list_item.get("image_url"),
            "page_number": list_item.get("page_number"),
            "item_index": item_index,
            "page_count": page_info.get("page_count"),
            "record_count": page_info.get("record_count"),
            "pagination_type": page_info.get("pagination_type"),
            "detail_url": detail_url,
        }

        return {
            "id": f"{self.site_id}:{external_id}",
            "site_id": self.site_id,
            "external_id": str(external_id) if external_id is not None else None,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": author or reporter or None,
            "publisher": source or "Ministry of National Defense of the People's Republic of China",
            "department": None,
            "journal": None,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": "Laws",
            "doi": None,
            "original_filename": original_filename,
            "metadata": _json_dumps(metadata),
        }

    def _article_info(self, soup):
        info = {}
        for span in soup.select("div.artichle-info p span"):
            label_node = span.find("em")
            label = _clean_text(label_node.get_text(" ", strip=True)).rstrip(":：").lower() if label_node else ""
            value = _clean_text(span.get_text(" ", strip=True))
            if label:
                value = re.sub(rf"^{re.escape(label)}\s*[:：]?\s*", "", value, flags=re.I)
            if label == "source":
                link = span.find("a")
                info["source"] = _clean_text(link.get_text(" ", strip=True)) if link else value
            elif label == "editor":
                info["editor"] = value
            elif label == "time":
                info["time"] = value
        return info

    def _find_pdf_url(self, soup, detail_url):
        for link in soup.find_all("a", href=True):
            href = link.get("href")
            if not href:
                continue
            clean_href = href.split("?", 1)[0].split("#", 1)[0].lower()
            if clean_href.endswith(".pdf"):
                return urljoin(detail_url, href)
        return None
