# -*- coding: utf-8 -*-
"""Crawler for Greek Ministry of Justice press releases.

Target list page:
    https://ministryofjustice.gr/?page_id=598

The WordPress REST/category/feed endpoints are blocked by the site's edge
rules, but the public page exposes a stable paginated HTML list:
    https://ministryofjustice.gr/?paged=<N>&page_id=598
Detail pages use numeric WordPress IDs:
    https://ministryofjustice.gr/?p=<post_id>
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from datetime import datetime
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class MinistryOfJusticeGrCrawler(BaseCrawler):
    site_id = "ministryofjustice-gr"
    site_name = "Custom: ministryofjustice-gr"
    base_url = "https://ministryofjustice.gr"

    START_URL = "https://ministryofjustice.gr/?page_id=598"
    PAGE_ID = "598"
    CATEGORY_ID = "203"
    CATEGORY_NAME = "ΔΕΛΤΙΑ ΤΥΠΟΥ"
    PUBLISHER = "Υπουργείο Δικαιοσύνης"

    _BACKOFFS = (1, 3, 9)
    _CURL_TIMEOUT = 45
    _MAX_PAGES = 200
    _MAX_WALL_SECONDS = 25 * 60
    _MIN_ABSTRACT_CHARS = 100
    _CURL_MARKER = "__MINISTRYOFJUSTICE_GR_CURL_META__:"
    _USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls: set[str] = set()
        started_at = time.time()
        limit_or_inf = limit if limit is not None else "inf"

        while True:
            if limit is not None and saved >= limit:
                break
            if page > self._MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached; stopping")
                break
            if time.time() - started_at >= self._MAX_WALL_SECONDS:
                print(f"[{self.site_id}] wall-clock budget nearly reached; stopping cleanly")
                break

            if page == 1 or page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            list_url = self._list_url(page)
            raw = self._curl_get(list_url, context=f"list page {page}")
            if not raw:
                print(f"[{self.site_id}] list page {page} failed; stopping")
                break

            soup = _make_soup(raw)
            if soup is None:
                print(f"[{self.site_id}] list page {page} could not be parsed; stopping")
                break

            items = _parse_list_items(soup, list_url)
            if not items:
                print(f"[{self.site_id}] page {page} returned no records; done")
                break

            new_items = [item for item in items if item["url"] not in seen_urls]
            if not new_items:
                print(f"[{self.site_id}] page {page} returned 0 new records; done")
                break

            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                url = item["url"]
                seen_urls.add(url)
                try:
                    if self._delay:
                        time.sleep(self._delay)
                    if self._process_item(item, page):
                        saved += 1
                        count = f"{saved}/{limit}" if limit is not None else str(saved)
                        print(f"[{self.site_id}] saved {count}: {item.get('title', '')[:80]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    item_label = item.get("post_id") or url
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break
            if not _has_next_page(soup, page):
                print(f"[{self.site_id}] next page link absent after page {page}; done")
                break

            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    def _process_item(self, item: dict, page: int) -> int:
        url = item["url"]
        post_id = item.get("post_id") or _extract_post_id(url)

        detail_raw = self._curl_get(url, context=f"detail {post_id or url}")
        if not detail_raw:
            print(f"[{self.site_id}] detail failed after retries: {post_id or url}")
            return 0

        soup = _make_soup(detail_raw)
        if soup is None:
            print(f"[{self.site_id}] detail parse failed: {post_id or url}")
            return 0

        detail = _parse_detail(soup, url)
        title = detail.get("title") or item.get("title") or "(untitled)"
        abstract = detail.get("abstract") or ""

        if len(abstract) < 50:
            print(f"[{self.site_id}] item {post_id or url} skipped: abstract {len(abstract)} chars < 50")
            return 0
        if len(abstract) < self._MIN_ABSTRACT_CHARS:
            print(
                f"[{self.site_id}] item {post_id or url} skipped: "
                f"abstract {len(abstract)} chars < {self._MIN_ABSTRACT_CHARS}"
            )
            return 0

        listed_date = item.get("listed_date")
        published_date = detail.get("published_date") or listed_date
        pdf_url = detail.get("pdf_url")
        original_filename = detail.get("original_filename") or _filename_from_url(pdf_url)
        external_id = str(post_id) if post_id else _slug_from_url(url)

        metadata = {
            "source": "WordPress paginated HTML list and detail pages",
            "list_url": item.get("list_url"),
            "detail_url": url,
            "api_list_endpoint": self._list_url(page),
            "api_detail_endpoint": url,
            "post_id": post_id,
            "wordpress_post_id": post_id,
            "node_id": post_id,
            "category_id": self.CATEGORY_ID,
            "category_raw": item.get("category") or detail.get("category"),
            "posted_date": item.get("listed_date_raw") or listed_date,
            "listed_date": listed_date,
            "listed_date_raw": item.get("listed_date_raw"),
            "published_time_raw": detail.get("published_time_raw"),
            "modified_time_raw": detail.get("modified_time_raw"),
            "author_raw": detail.get("author"),
            "keywords_raw": detail.get("keywords"),
            "og_description": detail.get("og_description"),
            "list_title": item.get("title"),
            "detail_title": detail.get("title"),
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
        }

        paper = {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, url)),
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": str(post_id) if post_id else external_id,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": detail.get("author"),
            "publisher": self.PUBLISHER,
            "department": None,
            "journal": None,
            "url": url,
            "pdf_url": pdf_url,
            "keywords": detail.get("keywords") or self.CATEGORY_NAME,
            "category": detail.get("category") or item.get("category") or self.CATEGORY_NAME,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }
        self._save_paper(paper)
        return 1

    def _list_url(self, page: int) -> str:
        if page <= 1:
            return self.START_URL
        return f"{self.base_url}/?paged={page}&page_id={self.PAGE_ID}"

    def _curl_get(self, url: str, context: str = "request") -> str | None:
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--connect-timeout",
            "15",
            "--max-time",
            str(self._CURL_TIMEOUT),
            "-A",
            self._USER_AGENT,
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "-H",
            "Accept-Language: el-GR,el;q=0.9,en-US;q=0.7,en;q=0.5",
            "-w",
            "\n" + self._CURL_MARKER + "%{http_code}\t%{url_effective}",
            url,
        ]

        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self._CURL_TIMEOUT + 10,
                )
                stdout = (result.stdout or b"").decode("utf-8", errors="replace")
                body, http_code, _effective_url = self._split_curl_output(stdout, url)

                if result.returncode != 0:
                    stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if http_code and int(http_code) >= 400:
                    raise RuntimeError(f"HTTP {http_code}")
                if not body.strip():
                    raise RuntimeError("empty response")
                return body
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                last_error = str(exc)
                print(f"[{self.site_id}] {context} curl attempt {attempt}/3 failed: {last_error}")
                if attempt < 3:
                    wait = self._BACKOFFS[attempt - 1]
                    print(f"[{self.site_id}] retrying in {wait}s...")
                    time.sleep(wait)

        print(f"[{self.site_id}] {context} failed after 3 attempts: {last_error}")
        return None

    def _split_curl_output(self, raw: str, fallback_url: str) -> tuple[str, str, str]:
        marker = "\n" + self._CURL_MARKER
        marker_pos = raw.rfind(marker)
        if marker_pos == -1:
            return raw, "", fallback_url

        body = raw[:marker_pos]
        meta = raw[marker_pos + len(marker):].strip()
        if "\t" not in meta:
            return body, "", fallback_url

        http_code, effective_url = meta.split("\t", 1)
        return body, http_code.strip(), effective_url.strip() or fallback_url


def _make_soup(raw: str):
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw or "", parser)
        except Exception:
            continue
    return None


def _parse_list_items(soup, list_url: str) -> list[dict]:
    items: list[dict] = []
    for article in soup.select("div.post-list article"):
        post_div = article.find(id=re.compile(r"^post-\d+$"))
        if not post_div:
            continue

        post_id = _extract_post_id(post_div.get("id", ""))
        title_link = article.select_one("h4.entry-title a[href]")
        image_link = article.select_one("a.image-post[href]")
        link = title_link or image_link
        if not link:
            continue

        url = urljoin(list_url, link.get("href", ""))
        title = _clean_text(title_link.get_text(" ", strip=True) if title_link else "")
        title = re.sub(r"\s*\.\.\.$", "", title).strip()
        if not title:
            img = article.find("img", alt=True)
            title = _clean_text(img.get("alt", "")) if img else ""

        listed_date_raw = _clean_text(article.select_one(".posted-on").get_text(" ", strip=True)) if article.select_one(".posted-on") else ""
        category = _clean_text(article.select_one(".cat-links").get_text(" ", strip=True)) if article.select_one(".cat-links") else ""

        items.append({
            "post_id": post_id or _extract_post_id(url),
            "title": title,
            "url": url,
            "listed_date_raw": listed_date_raw,
            "listed_date": _parse_greek_numeric_date(listed_date_raw),
            "category": category or None,
            "list_url": list_url,
        })
    return items


def _parse_detail(soup, url: str) -> dict:
    article = soup.select_one("article[id^='post-']") or soup.select_one("section#main article")
    content = article.select_one(".entry-content") if article else soup.select_one(".entry-content")

    if content:
        for node in content.select("script, style, noscript, div.post-views, .post-views"):
            node.decompose()
        abstract = _clean_text(content.get_text(" ", strip=True))
    else:
        abstract = ""

    title = _meta_content(soup, "og:title")
    if not title:
        title_node = article.select_one("h1.entry-title, h2.entry-title, .entry-title") if article else None
        title = _clean_text(title_node.get_text(" ", strip=True)) if title_node else ""
    else:
        title = _clean_text(title)

    published_time_raw = _meta_content(soup, "article:published_time") or _meta_content(soup, "datePublished", attr="itemprop")
    modified_time_raw = _meta_content(soup, "article:modified_time")
    category = _meta_content(soup, "article:section")

    keywords = []
    for meta in soup.find_all("meta"):
        prop = meta.get("property") or meta.get("name")
        if prop == "article:tag" and meta.get("content"):
            keywords.append(_clean_text(meta["content"]))
    if not keywords and category:
        keywords.append(_clean_text(category))

    author = (
        _meta_content(soup, "article:author:username")
        or _meta_content(soup, "author", attr="itemprop")
        or _join_author_parts(
            _meta_content(soup, "profile:first_name"),
            _meta_content(soup, "profile:last_name"),
        )
    )

    pdf_url = None
    if article:
        for a_tag in article.find_all("a", href=True):
            href = a_tag["href"]
            if ".pdf" in href.lower():
                pdf_url = urljoin(url, href)
                break

    original_filename = _filename_from_url(pdf_url)

    return {
        "title": title,
        "abstract": abstract,
        "published_date": _parse_iso_date(published_time_raw),
        "published_time_raw": published_time_raw,
        "modified_time_raw": modified_time_raw,
        "author": _clean_text(author) if author else None,
        "category": _clean_text(category) if category else None,
        "keywords": ", ".join(dict.fromkeys(k for k in keywords if k)) or None,
        "pdf_url": pdf_url,
        "original_filename": original_filename,
        "og_description": _meta_content(soup, "og:description"),
    }


def _has_next_page(soup, page: int) -> bool:
    expected = str(page + 1)
    for link in soup.select(".pagenavi a.page-numbers[href], .pagenavbar a.page-numbers[href]"):
        label = _clean_text(link.get_text(" ", strip=True))
        href = unescape(link.get("href", ""))
        if label == expected:
            return True
        if re.search(rf"[?&]paged={re.escape(expected)}(?:&|$)", href):
            return True
    return False


def _meta_content(soup, key: str, attr: str = "property") -> str | None:
    meta = soup.find("meta", attrs={attr: key})
    if meta and meta.get("content"):
        return meta["content"]
    if attr != "name":
        meta = soup.find("meta", attrs={"name": key})
        if meta and meta.get("content"):
            return meta["content"]
    return None


def _join_author_parts(first: str | None, last: str | None) -> str | None:
    parts = [_clean_text(part) for part in (first, last) if _clean_text(part)]
    return " ".join(parts) if parts else None


def _clean_text(value: str | None) -> str:
    text = unescape(str(value or "")).replace("\xa0", " ").replace("\u200b", "")
    return re.sub(r"\s+", " ", text).strip()


def _parse_greek_numeric_date(value: str | None) -> str | None:
    text = _clean_text(value)
    match = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", text)
    if not match:
        return None
    day, month, year = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _parse_iso_date(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", value)
    if match:
        return match.group(0)
    return _parse_greek_numeric_date(value)


def _extract_post_id(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"(?:post-|[?&]p=)(\d+)", value)
    if match:
        return match.group(1)
    match = re.search(r"/(\d+)(?:[-/]|$)", value)
    return match.group(1) if match else None


def _slug_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.query:
        return parsed.query
    path = parsed.path.strip("/")
    return path or None


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urlparse(url).path
    filename = unquote(path.rstrip("/").split("/")[-1])
    if "." in filename and len(filename) <= 240:
        return filename
    return None
