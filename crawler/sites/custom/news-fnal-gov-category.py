# -*- coding: utf-8 -*-
"""Crawler for Fermilab newsroom press releases.

Starting URL:
    https://news.fnal.gov/category/newsroom/press-release/

List API:
    https://news.fnal.gov/wp-json/wp/v2/posts?categories=55&per_page=100&page=N&_embed=1

Detail API:
    https://news.fnal.gov/wp-json/wp/v2/posts/{id}?_embed=1
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import sys
import time
from email.message import Message
from urllib.parse import unquote, urljoin, urlparse

# Absolute import: spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler


_SITE_ID = "news-fnal-gov-category"
_BASE_URL = "https://news.fnal.gov"
_START_URL = f"{_BASE_URL}/category/newsroom/press-release/"
_CATEGORY_ID = 55
_PER_PAGE = 100
_MAX_PAGES = 200
_MAX_WALL_SECONDS = 25 * 60
_DETAIL_DELAY_SECONDS = 1.0
_BACKOFFS = (1, 3, 9)
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _clean_space(value: str | None) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = text.replace("\xa0", " ").replace("\u200b", "")
    text = re.sub(r"\s+", " ", text, flags=re.MULTILINE).strip()
    return text


def _make_soup(raw: str | bytes | None, context: str = ""):
    if raw is None:
        raw = ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")

    try:
        from bs4 import BeautifulSoup
    except Exception as exc:
        print(f"[{_SITE_ID}] BeautifulSoup unavailable for {context}: {exc}")
        return None

    last_exc = None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception as exc:
            last_exc = exc
            print(f"[{_SITE_ID}] BeautifulSoup({parser}) failed for {context}: {exc}")
    print(f"[{_SITE_ID}] all BeautifulSoup parsers failed for {context}: {last_exc}")
    return None


def _html_to_text(raw_html: str | None, context: str = "") -> str:
    if not raw_html:
        return ""
    soup = _make_soup(raw_html, context)
    if soup is None:
        text = re.sub(r"<[^>]+>", " ", raw_html)
        return _clean_space(text)

    for node in soup.find_all(["script", "style", "noscript", "iframe"]):
        node.decompose()
    return _clean_space(soup.get_text(" ", strip=True))


def _date_only(raw: str | None) -> str | None:
    if not raw:
        return None
    match = re.search(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b", raw)
    if match:
        return match.group(0)
    return None


def _list_api_url(page: int) -> str:
    return (
        f"{_BASE_URL}/wp-json/wp/v2/posts"
        f"?categories={_CATEGORY_ID}&per_page={_PER_PAGE}&page={page}&_embed=1"
    )


def _detail_api_url(post_id: str | int) -> str:
    return f"{_BASE_URL}/wp-json/wp/v2/posts/{post_id}?_embed=1"


def _curl_text(url: str, *, timeout: int = 45, retries: int = 3):
    """Fetch URL with curl and return (status_code, body) or (None, None)."""
    marker = "\n__FNAL_HTTP_CODE__:"
    cmd = [
        "curl",
        "--tls-max",
        "1.3",
        "-skL",
        "--compressed",
        "--connect-timeout",
        "20",
        "--max-time",
        str(timeout),
        "-H",
        f"User-Agent: {_UA}",
        "-H",
        "Accept: application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "-H",
        "Accept-Language: en-US,en;q=0.9",
        "-w",
        marker + "%{http_code}",
        url,
    ]

    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
            body = result.stdout.decode("utf-8", errors="replace")
            status = None
            if marker in body:
                body, code = body.rsplit(marker, 1)
                try:
                    status = int(code.strip()[:3])
                except ValueError:
                    status = None

            if result.returncode == 0 and status is not None and status < 500:
                return status, body

            err = result.stderr.decode("utf-8", errors="replace").strip()
            print(
                f"[{_SITE_ID}] fetch failed (attempt {attempt + 1}/{retries}) "
                f"status={status} rc={result.returncode} url={url} {err}"
            )
        except Exception as exc:
            print(f"[{_SITE_ID}] fetch error (attempt {attempt + 1}/{retries}) for {url}: {exc}")

        if attempt < retries - 1:
            time.sleep(_BACKOFFS[attempt])

    print(f"[{_SITE_ID}] all {retries} fetch attempts failed for {url}")
    return None, None


def _curl_json(url: str, *, timeout: int = 45, retries: int = 3):
    status, body = _curl_text(url, timeout=timeout, retries=retries)
    if status is None:
        return None, None
    if status >= 400:
        try:
            payload = json.loads(body or "{}")
        except Exception:
            payload = None
        return status, payload
    try:
        return status, json.loads(body or "")
    except Exception as exc:
        print(f"[{_SITE_ID}] JSON parse failed for {url}: {exc}")
        return status, None


def _curl_head(url: str, *, timeout: int = 30, retries: int = 3) -> str:
    cmd = [
        "curl",
        "--tls-max",
        "1.3",
        "-skLI",
        "--connect-timeout",
        "15",
        "--max-time",
        str(timeout),
        "-H",
        f"User-Agent: {_UA}",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            if result.returncode == 0 and result.stdout:
                return result.stdout.decode("utf-8", errors="replace")
            err = result.stderr.decode("utf-8", errors="replace").strip()
            print(
                f"[{_SITE_ID}] HEAD failed (attempt {attempt + 1}/{retries}) "
                f"rc={result.returncode} url={url} {err}"
            )
        except Exception as exc:
            print(f"[{_SITE_ID}] HEAD error (attempt {attempt + 1}/{retries}) for {url}: {exc}")
        if attempt < retries - 1:
            time.sleep(_BACKOFFS[attempt])
    return ""


def _extract_terms(post: dict):
    categories: list[str] = []
    tags: list[str] = []
    embedded_terms = post.get("_embedded", {}).get("wp:term", [])
    if isinstance(embedded_terms, list):
        for group in embedded_terms:
            if not isinstance(group, list):
                continue
            for term in group:
                if not isinstance(term, dict):
                    continue
                name = _clean_space(term.get("name"))
                if not name:
                    continue
                taxonomy = term.get("taxonomy")
                if taxonomy == "category" and name not in categories:
                    categories.append(name)
                elif taxonomy == "post_tag" and name not in tags:
                    tags.append(name)
    return categories, tags


def _article_schema(post: dict) -> dict:
    graph = (
        post.get("yoast_head_json", {})
        .get("schema", {})
        .get("@graph", [])
    )
    if not isinstance(graph, list):
        return {}
    for node in graph:
        if not isinstance(node, dict):
            continue
        node_type = node.get("@type")
        if node_type == "Article" or (isinstance(node_type, list) and "Article" in node_type):
            return node
    return {}


def _extract_authors(post: dict) -> str | None:
    authors: list[str] = []

    article = _article_schema(post)
    schema_author = article.get("author")
    if isinstance(schema_author, dict):
        name = _clean_space(schema_author.get("name"))
        if name:
            authors.append(name)
    elif isinstance(schema_author, list):
        for item in schema_author:
            if isinstance(item, dict):
                name = _clean_space(item.get("name"))
            else:
                name = _clean_space(str(item))
            if name and name not in authors:
                authors.append(name)

    yoast_author = _clean_space(post.get("yoast_head_json", {}).get("author"))
    if yoast_author and yoast_author not in authors:
        authors.append(yoast_author)

    for item in post.get("_embedded", {}).get("author", []) or []:
        if isinstance(item, dict):
            name = _clean_space(item.get("name"))
            if name and name not in authors:
                authors.append(name)

    return "; ".join(authors) if authors else None


def _extract_keywords(post: dict, tags: list[str]) -> str | None:
    keywords: list[str] = []
    raw = _article_schema(post).get("keywords")
    if isinstance(raw, list):
        keywords.extend(_clean_space(x) for x in raw)
    elif isinstance(raw, str):
        keywords.extend(_clean_space(x) for x in raw.split(","))
    keywords.extend(tags)

    deduped: list[str] = []
    for item in keywords:
        if item and item not in deduped:
            deduped.append(item)
    return ", ".join(deduped) if deduped else None


def _extract_pdf_url(post: dict) -> str | None:
    html_parts = [
        (post.get("content") or {}).get("rendered", ""),
        (post.get("excerpt") or {}).get("rendered", ""),
    ]
    for raw_html in html_parts:
        if not raw_html:
            continue
        soup = _make_soup(raw_html, "pdf-links")
        if soup is None:
            for href in re.findall(r"""href=["']([^"']+\.pdf(?:\?[^"']*)?)["']""", raw_html, flags=re.I):
                return urljoin(_BASE_URL, html.unescape(href))
            continue
        for link in soup.find_all("a", href=True):
            href = html.unescape(link.get("href", "").strip())
            if re.search(r"\.pdf(?:$|[?#])", href, flags=re.I):
                return urljoin(_BASE_URL, href)
    return None


def _filename_from_content_disposition(headers: str) -> str | None:
    if not headers:
        return None
    for block in re.split(r"\r?\n\r?\n", headers.strip()):
        msg = Message()
        for line in block.splitlines():
            if ":" not in line or line.upper().startswith("HTTP/"):
                continue
            key, value = line.split(":", 1)
            msg[key.strip()] = value.strip()
        value = msg.get("Content-Disposition")
        if not value:
            continue
        match = re.search(r"filename\*=UTF-8''([^;]+)", value, flags=re.I)
        if match:
            return unquote(match.group(1).strip().strip('"'))
        match = re.search(r'filename="?([^";]+)"?', value, flags=re.I)
        if match:
            return match.group(1).strip()
    return None


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    tail = unquote(urlparse(url).path.rstrip("/").split("/")[-1])
    if tail and "." in tail and len(tail) <= 200:
        return tail
    return None


def _original_filename(pdf_url: str | None) -> str | None:
    if not pdf_url:
        return None
    filename = _filename_from_url(pdf_url)
    if filename:
        return filename
    return _filename_from_content_disposition(_curl_head(pdf_url))


def _build_metadata(post: dict, *, categories: list[str], tags: list[str], pdf_url: str | None,
                    original_filename: str | None, list_api_url: str, detail_api_url: str) -> str:
    article = _article_schema(post)
    raw = {
        "node_id": post.get("id"),
        "post_id": post.get("id"),
        "slug": post.get("slug"),
        "status": post.get("status"),
        "type": post.get("type"),
        "guid": (post.get("guid") or {}).get("rendered"),
        "date": post.get("date"),
        "date_gmt": post.get("date_gmt"),
        "modified": post.get("modified"),
        "modified_gmt": post.get("modified_gmt"),
        "author_id": post.get("author"),
        "featured_media": post.get("featured_media"),
        "categories": post.get("categories"),
        "tags": post.get("tags"),
        "category_names": categories,
        "tag_names": tags,
        "yoast_head_json": post.get("yoast_head_json"),
        "meta": post.get("meta"),
        "acf": post.get("acf"),
        "class_list": post.get("class_list"),
        "links": post.get("_links"),
        "content_html": (post.get("content") or {}).get("rendered", ""),
        "excerpt_html": (post.get("excerpt") or {}).get("rendered", ""),
        "list_api_url": list_api_url,
        "detail_api_url": detail_api_url,
        "start_url": _START_URL,
    }
    metadata = {
        "posted_date": post.get("date"),
        "originalFilename": original_filename,
        "journal_raw": None,
        "series": None,
        "volume": None,
        "issue": None,
        "node_id": post.get("id"),
        "wp_post_id": post.get("id"),
        "wp_slug": post.get("slug"),
        "wp_categories": categories,
        "wp_tags": tags,
        "article_published_time": post.get("yoast_head_json", {}).get("article_published_time"),
        "article_modified_time": post.get("yoast_head_json", {}).get("article_modified_time"),
        "article_word_count": article.get("wordCount"),
        "pdf_url_detected": pdf_url,
        "raw": raw,
    }
    return json.dumps(metadata, ensure_ascii=False)


def _post_to_paper(post: dict, *, list_api_url: str, detail_api_url: str) -> dict | None:
    post_id = post.get("id")
    if post_id is None:
        print(f"[{_SITE_ID}] item missing native id, skipping")
        return None

    post_id_str = str(post_id)
    title = _html_to_text((post.get("title") or {}).get("rendered", ""), f"title {post_id_str}")
    if not title:
        print(f"[{_SITE_ID}] item {post_id_str} missing title, skipping")
        return None

    categories, tags = _extract_terms(post)
    keywords = _extract_keywords(post, tags)

    excerpt = _html_to_text((post.get("excerpt") or {}).get("rendered", ""), f"excerpt {post_id_str}")
    content_text = _html_to_text((post.get("content") or {}).get("rendered", ""), f"content {post_id_str}")
    abstract = excerpt if len(excerpt) >= 50 else content_text
    if len(abstract) < 50:
        print(f"[{_SITE_ID}] item {post_id_str} abstract too short ({len(abstract)} chars), skipping")
        return None

    published_date = (
        _date_only(post.get("yoast_head_json", {}).get("article_published_time"))
        or _date_only(post.get("date_gmt"))
        or _date_only(post.get("date"))
    )
    listed_date = _date_only(post.get("date"))
    if not listed_date:
        listed_date = published_date

    url = post.get("link") or (post.get("yoast_head_json", {}) or {}).get("canonical")
    if not url:
        print(f"[{_SITE_ID}] item {post_id_str} missing URL, skipping")
        return None

    pdf_url = _extract_pdf_url(post)
    original_filename = _original_filename(pdf_url)
    authors = _extract_authors(post)
    category = ", ".join(categories) if categories else "Press release"
    metadata = _build_metadata(
        post,
        categories=categories,
        tags=tags,
        pdf_url=pdf_url,
        original_filename=original_filename,
        list_api_url=list_api_url,
        detail_api_url=detail_api_url,
    )

    return {
        "id": f"{_SITE_ID}:{post_id_str}",
        "site_id": _SITE_ID,
        "external_id": post_id_str,
        "post_number": post_id_str,
        "title": title,
        "abstract": abstract,
        "published_date": published_date,
        "listed_date": listed_date,
        "posted_date": listed_date,
        "authors": authors,
        "publisher": "Fermi National Accelerator Laboratory",
        "department": "Newsroom",
        "journal": None,
        "url": url,
        "pdf_url": pdf_url,
        "keywords": keywords,
        "category": category,
        "doi": None,
        "original_filename": original_filename,
        "metadata": metadata,
    }


class NewsFnalGovCategoryCrawler(BaseCrawler):
    site_id = "news-fnal-gov-category"
    site_name = "Custom: news-fnal-gov-category"
    base_url = "https://news.fnal.gov"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

        for page in range(1, _MAX_PAGES + 1):
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time > _MAX_WALL_SECONDS:
                print(f"[{self.site_id}] wall-clock budget reached, stopping cleanly")
                break
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            list_url = _list_api_url(page)
            status, payload = _curl_json(list_url)
            if status is None:
                print(f"[{self.site_id}] page {page}: fetch failed, stopping")
                break
            if status == 400 and isinstance(payload, dict) and payload.get("code") == "rest_post_invalid_page_number":
                print(f"[{self.site_id}] page {page}: beyond last WordPress page, stopping")
                break
            if status >= 400:
                print(f"[{self.site_id}] page {page}: HTTP {status}, stopping")
                break
            if not isinstance(payload, list) or not payload:
                print(f"[{self.site_id}] page {page}: 0 records, stopping")
                break

            new_items = []
            for item in payload:
                url = item.get("link") if isinstance(item, dict) else None
                native_id = item.get("id") if isinstance(item, dict) else None
                dedupe_key = url or (str(native_id) if native_id is not None else None)
                if not dedupe_key:
                    continue
                if dedupe_key in seen_urls:
                    continue
                seen_urls.add(dedupe_key)
                new_items.append(item)

            if not new_items:
                print(f"[{self.site_id}] page {page}: all records already seen, stopping")
                break

            for item in new_items:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > _MAX_WALL_SECONDS:
                    print(f"[{self.site_id}] wall-clock budget reached during items, stopping cleanly")
                    return saved

                native_id = item.get("id")
                item_label = native_id if native_id is not None else item.get("link", "?")
                try:
                    detail_url = _detail_api_url(native_id)
                    time.sleep(getattr(self, "_delay", _DETAIL_DELAY_SECONDS))
                    detail_status, detail = _curl_json(detail_url)
                    if detail_status is None:
                        print(f"[{self.site_id}] item {item_label} detail fetch failed, skipping")
                        continue
                    if detail_status >= 400 or not isinstance(detail, dict):
                        print(f"[{self.site_id}] item {item_label} detail HTTP {detail_status}, skipping")
                        continue

                    paper = _post_to_paper(detail, list_api_url=list_url, detail_api_url=detail_url)
                    if not paper:
                        continue
                    self._save_paper(paper)
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

        else:
            print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached, stopping")

        print(f"[{self.site_id}] done: saved {saved}/{limit_or_inf}")
        return saved
