# -*- coding: utf-8 -*-
"""EMSL science reports crawler.

Starting URL: https://www.emsl.pnnl.gov/science/reports
List endpoint: Drupal Views listing at /science/reports?page=N
AJAX endpoint: Drupal Views AJAX POST at /views/ajax
  view_name=reports_listing, view_display_id=embed, view_path=/node/15222
Detail behavior: report rows link directly to PDF files. Drupal /node/<id>
  redirects back to /science/reports, so the PDF URL is the record URL.
"""

from __future__ import annotations

import email.utils
import json
import re
import subprocess
import sys
import time
import urllib.parse
import uuid
from datetime import datetime

# Absolute import: spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler


_SITE_ID = "emsl-pnnl-gov-science"
_BASE_URL = "https://www.emsl.pnnl.gov"
_START_URL = f"{_BASE_URL}/science/reports"
_AJAX_URL = f"{_BASE_URL}/views/ajax"
_VIEW_NAME = "reports_listing"
_VIEW_DISPLAY_ID = "embed"
_VIEW_PATH = "/node/15222"
_DEFAULT_VIEW_DOM_ID = "6ac007463537c58902b950d57c029a886a3c79cb0ea4518492158ed355d65b5d"
_MAX_PAGES = 200
_MAX_WALL_SECONDS = 25 * 60
_MIN_ABSTRACT_CHARS = 50
_PUBLISHER = "Environmental Molecular Sciences Laboratory; Pacific Northwest National Laboratory"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _curl(
    url: str,
    *,
    method: str = "GET",
    data: str | None = None,
    headers: list[str] | None = None,
    timeout: int = 30,
    retries: int = 3,
) -> str | None:
    """Fetch with curl, TLS max 1.3, and exponential retry backoff."""
    base_cmd = [
        "curl",
        "--tls-max",
        "1.3",
        "-skL",
        "--compressed",
        "--max-time",
        str(timeout),
        "-H",
        f"User-Agent: {_USER_AGENT}",
        "-H",
        "Accept-Language: en-US,en;q=0.9",
        "-H",
        f"Referer: {_START_URL}",
    ]
    for header in headers or []:
        base_cmd.extend(["-H", header])
    if method == "HEAD":
        base_cmd.append("-I")
    elif method == "POST":
        base_cmd.extend(["-X", "POST", "--data", data or ""])
    base_cmd.append(url)

    waits = (1, 3, 9)
    for attempt in range(retries):
        try:
            result = subprocess.run(
                base_cmd,
                capture_output=True,
                timeout=timeout + 10,
            )
            body = result.stdout.decode("utf-8", errors="replace")
            if result.returncode == 0 and body.strip():
                return body
            message = result.stderr.decode("utf-8", errors="replace").strip()
            if attempt < retries - 1:
                wait = waits[min(attempt, len(waits) - 1)]
                print(
                    f"[{_SITE_ID}] {method} {url} failed/empty "
                    f"(attempt {attempt + 1}/{retries}): {message}; retry in {wait}s"
                )
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = waits[min(attempt, len(waits) - 1)]
                print(
                    f"[{_SITE_ID}] {method} {url} error "
                    f"(attempt {attempt + 1}/{retries}): {exc}; retry in {wait}s"
                )
                time.sleep(wait)
            else:
                print(f"[{_SITE_ID}] {method} {url} failed after {retries} attempts: {exc}")
    return None


def _curl_get_html(url: str) -> str | None:
    return _curl(
        url,
        headers=[
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
        ],
    )


def _curl_head(url: str) -> str | None:
    return _curl(
        url,
        method="HEAD",
        headers=["Accept: application/pdf,text/html,*/*;q=0.8"],
        timeout=20,
    )


def _curl_post_ajax(page: int, ajax_config: dict) -> str | None:
    payload = {
        "view_name": ajax_config.get("view_name") or _VIEW_NAME,
        "view_display_id": ajax_config.get("view_display_id") or _VIEW_DISPLAY_ID,
        "view_args": ajax_config.get("view_args") or "",
        "view_path": ajax_config.get("view_path") or _VIEW_PATH,
        "view_base_path": ajax_config.get("view_base_path") or "",
        "view_dom_id": ajax_config.get("view_dom_id") or _DEFAULT_VIEW_DOM_ID,
        "pager_element": str(ajax_config.get("pager_element", 0)),
        "_drupal_ajax": "1",
        "page": str(page),
    }
    return _curl(
        _AJAX_URL,
        method="POST",
        data=urllib.parse.urlencode(payload),
        headers=[
            "Accept: application/json, text/javascript, */*; q=0.01",
            "Content-Type: application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With: XMLHttpRequest",
        ],
    )


def _make_soup(raw: str):
    """Parse HTML with html5lib, then lxml, then html.parser."""
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:
        print(f"[{_SITE_ID}] BeautifulSoup unavailable: {exc}")
        return None

    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw or "", parser)
        except Exception as exc:
            print(f"[{_SITE_ID}] BeautifulSoup parser {parser} failed: {exc}")
    return None


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _absolute_url(url: str | None) -> str | None:
    if not url:
        return None
    return urllib.parse.urljoin(_BASE_URL, url)


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urllib.parse.urlparse(url).path
    filename = urllib.parse.unquote(path.rstrip("/").split("/")[-1])
    return filename or None


def _parse_header_block(headers: str | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if not headers:
        return values
    for line in headers.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip().lower()] = value.strip()
    return values


def _filename_from_content_disposition(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', value, re.I)
    if not match:
        return None
    return urllib.parse.unquote(match.group(1).strip())


def _iso_from_http_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        return parsed.date().isoformat()
    except Exception:
        return None


def _date_from_pdf_path(url: str | None) -> tuple[str | None, str | None]:
    if not url:
        return None, None
    match = re.search(r"/(\d{4})-(\d{2})/", urllib.parse.urlparse(url).path)
    if not match:
        return None, None
    raw = f"{match.group(1)}-{match.group(2)}"
    return f"{raw}-01", raw


def _published_date_from_year(year: str | None) -> str | None:
    if year and re.fullmatch(r"\d{4}", year):
        return f"{year}-01-01"
    return None


def _post_number_from_url(url: str | None) -> str | None:
    if not url:
        return None
    filename = _filename_from_url(url)
    if filename:
        stem = filename.rsplit(".", 1)[0]
        return stem[:200] or None
    return None


def _extract_ajax_config(html: str | None) -> dict:
    if not html:
        return {}
    match = re.search(
        r'<script[^>]+data-drupal-selector=["\']drupal-settings-json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.S,
    )
    if not match:
        return {}
    try:
        settings = json.loads(match.group(1))
    except Exception:
        return {}
    ajax_views = settings.get("views", {}).get("ajaxViews", {})
    for config in ajax_views.values():
        if isinstance(config, dict) and config.get("view_name") == _VIEW_NAME:
            return dict(config)
    return {}


def _extract_ajax_html(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        commands = json.loads(raw)
    except Exception:
        return None
    best_html = ""
    for command in commands:
        if not isinstance(command, dict) or command.get("command") != "insert":
            continue
        data = command.get("data") or ""
        if "node--type--workshop-reports" in data:
            return data
        if len(data) > len(best_html):
            best_html = data
    return best_html or None


def _page_url(page: int) -> str:
    if page <= 0:
        return _START_URL
    return f"{_START_URL}?page={page}"


def _fetch_list_html(page: int, ajax_config: dict, initial_html: str | None = None) -> str | None:
    if page == 0 and initial_html:
        return initial_html

    if ajax_config:
        raw = _curl_post_ajax(page, ajax_config)
        ajax_html = _extract_ajax_html(raw)
        if ajax_html:
            return ajax_html

    return _curl_get_html(_page_url(page))


def _parse_page_number(href: str | None) -> int | None:
    if not href:
        return None
    parsed = urllib.parse.urlparse(href)
    query = urllib.parse.parse_qs(parsed.query)
    value = (query.get("page") or [None])[0]
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _has_next_page(soup, page: int) -> bool:
    if not soup:
        return False
    for link in soup.select("a.usa-pagination__next-page"):
        next_page = _parse_page_number(link.get("href"))
        if next_page is not None and next_page > page:
            return True
    return False


def _classify_tags(tags: list[str]) -> tuple[str | None, list[str], str | None, str | None]:
    year = None
    category = None
    science_area = None
    irp_values: list[str] = []

    report_types = {"Science reports", "Infrastructure"}
    science_areas = {
        "Environmental Transformations and Interactions",
        "Functional and Systems Biology",
        "Computing, Analytics, and Modeling",
    }

    for tag in tags:
        if re.fullmatch(r"\d{4}", tag):
            year = tag
        elif tag in report_types:
            category = tag
        elif tag in science_areas:
            science_area = tag
        else:
            irp_values.append(tag)

    return year, irp_values, science_area, category


def _parse_list_items(html: str | None, page: int) -> tuple[list[dict], bool]:
    soup = _make_soup(html or "")
    if soup is None:
        return [], False

    items: list[dict] = []
    for article in soup.select("article.node--type--workshop-reports"):
        link = article.select_one("h3 a[href]")
        if not link:
            continue
        pdf_url = _absolute_url(link.get("href"))
        title = _clean_text(link.get_text(" ", strip=True))
        if not pdf_url or not title:
            continue

        paragraph = article.find("p")
        abstract = _clean_text(paragraph.get_text(" ", strip=True) if paragraph else "")
        tags = [_clean_text(tag.get_text(" ", strip=True)) for tag in article.select(".usa-tag")]
        tags = [tag for tag in tags if tag]
        year, irp_values, science_area, category = _classify_tags(tags)
        node_id = article.get("data-history-node-id") or None

        items.append(
            {
                "title": title,
                "abstract": abstract,
                "pdf_url": pdf_url,
                "url": pdf_url,
                "node_id": node_id,
                "post_number": node_id or _post_number_from_url(pdf_url),
                "published_year": year,
                "published_date": _published_date_from_year(year),
                "tags": tags,
                "keywords": ", ".join([tag for tag in tags if not re.fullmatch(r"\d{4}", tag)]),
                "category": category,
                "science_area": science_area,
                "irp": "; ".join(irp_values) if irp_values else None,
                "list_page": _page_url(page),
                "list_page_index": page,
            }
        )

    return items, _has_next_page(soup, page)


def _fetch_pdf_metadata(pdf_url: str) -> dict:
    headers_raw = _curl_head(pdf_url)
    headers = _parse_header_block(headers_raw)
    content_disposition = headers.get("content-disposition")
    original_filename = (
        _filename_from_content_disposition(content_disposition)
        or _filename_from_url(pdf_url)
    )

    listed_date_raw = headers.get("last-modified")
    listed_date = _iso_from_http_date(listed_date_raw)
    path_listed_date, path_date_raw = _date_from_pdf_path(pdf_url)
    if not listed_date:
        listed_date = path_listed_date
        listed_date_raw = path_date_raw

    return {
        "listed_date": listed_date,
        "posted_date_raw": listed_date_raw,
        "original_filename": original_filename,
        "headers": {
            "content_type": headers.get("content-type"),
            "content_length": headers.get("content-length"),
            "last_modified": headers.get("last-modified"),
            "content_disposition": content_disposition,
        },
        "pdf_path_date_raw": path_date_raw,
    }


def _build_paper(item: dict) -> dict:
    pdf_meta = _fetch_pdf_metadata(item["pdf_url"])
    external_id = item.get("node_id") or item.get("post_number") or item["pdf_url"]
    post_number = item.get("post_number")
    original_filename = pdf_meta.get("original_filename")

    metadata = {
        "posted_date": pdf_meta.get("posted_date_raw") or item.get("published_year"),
        "listed_date": pdf_meta.get("listed_date"),
        "originalFilename": original_filename,
        "journal_raw": None,
        "series": None,
        "volume": None,
        "issue": None,
        "node_id": item.get("node_id"),
        "data_history_node_id": item.get("node_id"),
        "post_number": post_number,
        "published_year": item.get("published_year"),
        "tags": item.get("tags") or [],
        "science_area": item.get("science_area"),
        "irp": item.get("irp"),
        "report_type": item.get("category"),
        "list_page": item.get("list_page"),
        "list_page_index": item.get("list_page_index"),
        "view_name": _VIEW_NAME,
        "view_display_id": _VIEW_DISPLAY_ID,
        "pdf_headers": pdf_meta.get("headers") or {},
        "pdf_path_date_raw": pdf_meta.get("pdf_path_date_raw"),
        "record_url_kind": "pdf_direct",
    }

    return {
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, item["pdf_url"])),
        "site_id": _SITE_ID,
        "external_id": str(external_id),
        "post_number": str(post_number) if post_number else None,
        "title": item.get("title"),
        "abstract": item.get("abstract"),
        "published_date": item.get("published_date") or pdf_meta.get("listed_date"),
        "listed_date": pdf_meta.get("listed_date"),
        "posted_date": pdf_meta.get("listed_date"),
        "authors": None,
        "publisher": _PUBLISHER,
        "department": "Environmental Molecular Sciences Laboratory",
        "journal": None,
        "url": item["url"],
        "pdf_url": item["pdf_url"],
        "keywords": item.get("keywords"),
        "category": item.get("category"),
        "doi": None,
        "original_filename": original_filename,
        "metadata": json.dumps(metadata, ensure_ascii=False),
    }


class EmslPnnlGovScienceCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: emsl-pnnl-gov-science"
    base_url = _BASE_URL

    def crawl(self, limit=None):
        saved = 0
        page = 0
        seen_urls: set[str] = set()
        started_at = time.monotonic()
        limit_label = str(limit) if limit is not None else "inf"

        initial_html = _curl_get_html(_START_URL)
        ajax_config = _extract_ajax_config(initial_html)
        if not ajax_config:
            ajax_config = {
                "view_name": _VIEW_NAME,
                "view_display_id": _VIEW_DISPLAY_ID,
                "view_path": _VIEW_PATH,
                "view_dom_id": _DEFAULT_VIEW_DOM_ID,
                "pager_element": 0,
            }

        while page < _MAX_PAGES:
            elapsed = time.monotonic() - started_at
            if elapsed >= _MAX_WALL_SECONDS - 30:
                print(f"[{_SITE_ID}] wall-clock budget nearly exhausted; exiting cleanly")
                break

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_label}")

            html = _fetch_list_html(page, ajax_config, initial_html)
            items, has_next = _parse_list_items(html, page)
            if not items:
                print(f"[{_SITE_ID}] page {page}: no records; stopping")
                break

            new_records = 0
            for index, item in enumerate(items, start=1):
                if limit is not None and saved >= limit:
                    return saved

                record_url = item.get("url") or item.get("pdf_url")
                if not record_url:
                    continue
                if record_url in seen_urls:
                    print(f"[{_SITE_ID}] duplicate URL skipped: {record_url}")
                    continue
                seen_urls.add(record_url)
                new_records += 1

                try:
                    delay = self._delay if self._delay is not None else 1.0
                    if delay > 0:
                        time.sleep(delay)

                    paper = _build_paper(item)
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < _MIN_ABSTRACT_CHARS:
                        print(
                            f"[{_SITE_ID}] item {item.get('post_number') or index} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                except Exception as exc:
                    item_id = item.get("post_number") or item.get("url") or index
                    print(f"[{_SITE_ID}] item {item_id} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break
            if new_records == 0:
                print(f"[{_SITE_ID}] page {page}: 0 new records; stopping")
                break
            if not has_next:
                break

            page += 1

        if page >= _MAX_PAGES:
            print(f"[{_SITE_ID}] reached safety cap of {_MAX_PAGES} pages")
        return saved
