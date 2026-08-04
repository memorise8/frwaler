# -*- coding: utf-8 -*-
"""Crawler for BMZ German publications.

Start page: https://www.bmz.de/de/aktuelles/publikationen

The publication list is loaded by the site's "publications" component from:
    /ajax/filterlist/de/24710-24710?offset=N&limit=9

Each list item exposes a native content id in the PDF URL
(/resource/blob/{id}/...) and has a stable HTML wrapper at:
    /de/aktuelles/publikationen/{id}-{id}
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

# Absolute import: spec_from_file_location has no package context.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
from crawler.base_crawler import BaseCrawler  # noqa: E402

try:
    from curl_cffi import requests as _cffi_requests
    _CFFI_AVAILABLE = True
except ImportError:
    _CFFI_AVAILABLE = False


_SITE_ID = "bmz-de-de"
_BASE_URL = "https://www.bmz.de"
_START_URL = f"{_BASE_URL}/de/aktuelles/publikationen"
_LIST_ENDPOINT = f"{_BASE_URL}/ajax/filterlist/de/24710-24710"
_DETAIL_URL_TMPL = f"{_BASE_URL}/de/aktuelles/publikationen/{{content_id}}-{{content_id}}"
_PUBLISHER = "Bundesministerium fuer wirtschaftliche Zusammenarbeit und Entwicklung (BMZ)"
_PAGE_SIZE = 9
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_STOP_BEFORE_SECONDS = 30
_MIN_ABSTRACT_CHARS = 100
_RETRY_WAITS = (1, 3, 9)

_GERMAN_MONTHS = {
    "januar": "01",
    "jan": "01",
    "februar": "02",
    "feb": "02",
    "maerz": "03",
    "marz": "03",
    "maer": "03",
    "mrz": "03",
    "april": "04",
    "apr": "04",
    "mai": "05",
    "juni": "06",
    "jun": "06",
    "juli": "07",
    "jul": "07",
    "august": "08",
    "aug": "08",
    "september": "09",
    "sep": "09",
    "oktober": "10",
    "okt": "10",
    "november": "11",
    "nov": "11",
    "dezember": "12",
    "dez": "12",
}


def _clean_text(value):
    if not value:
        return ""
    text = unescape(str(value)).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _strip_html(value):
    if not value:
        return ""
    return _clean_text(re.sub(r"<[^>]+>", " ", value))


def _norm_month(value):
    return (
        value.lower()
        .replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
        .strip(". ")
    )


def _parse_date(raw):
    """Return an ISO-ish date. Month-only dates are represented as YYYY-MM-01."""
    text = _clean_text(raw)
    if not text:
        return None
    text = text.replace("Sachstandsdatum", " ")
    text = _clean_text(text)

    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    m = re.search(r"(\d{1,2})[.](\d{1,2})[.](\d{4})", text)
    if m:
        day, month, year = m.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"

    m = re.search(r"(\d{1,2})/(\d{4})", text)
    if m:
        month, year = m.groups()
        return f"{year}-{int(month):02d}-01"

    m = re.search(r"(\d{1,2})\s+([A-Za-zÄÖÜäöüß.]+)\s+(\d{4})", text)
    if m:
        day, month_name, year = m.groups()
        month = _GERMAN_MONTHS.get(_norm_month(month_name))
        if month:
            return f"{year}-{month}-{int(day):02d}"

    m = re.search(r"([A-Za-zÄÖÜäöüß.]+)\s+(\d{4})", text)
    if m:
        month_name, year = m.groups()
        month = _GERMAN_MONTHS.get(_norm_month(month_name))
        if month:
            return f"{year}-{month}-01"

    m = re.search(r"\b(20\d{2}|19\d{2})\b", text)
    if m:
        return f"{m.group(1)}-01-01"

    return None


def _filename_from_url(url):
    if not url:
        return None
    path = urlparse(url).path
    tail = unquote(path.rstrip("/").split("/")[-1])
    if tail and "." in tail and len(tail) <= 240:
        return tail
    return None


def _content_id_from_url(url):
    if not url:
        return None
    m = re.search(r"/resource/(?:blob|crblob)/(\d+)/", url)
    if m:
        return m.group(1)
    m = re.search(r"/(\d+)(?:-\d+)?(?:[/?#]|$)", url)
    return m.group(1) if m else None


def _definition_value(container, term):
    """Extract text from role=definition blocks such as 'Sachstandsdatum 05/2026'."""
    wanted = term.lower()
    for definition in container.find_all(attrs={"role": "definition"}):
        text = _clean_text(definition.get_text(" ", strip=True))
        if text.lower().startswith(wanted):
            return _clean_text(text[len(term):])
    return ""


def _class_text(container, selector):
    node = container.select_one(selector)
    return _clean_text(node.get_text(" ", strip=True)) if node else ""


class BmzDeDeCrawler(BaseCrawler):
    site_id = "bmz-de-de"
    site_name = "Custom: bmz-de-de"
    base_url = "https://www.bmz.de"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self._detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network and parsing helpers
    # ------------------------------------------------------------------

    def _cffi_get(self, url, *, context="", timeout=45):
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
            "Referer": _START_URL,
        }
        last_error = "unknown error"
        for attempt, wait in enumerate(_RETRY_WAITS, start=1):
            try:
                r = _cffi_requests.get(url, headers=headers, impersonate="chrome124", timeout=timeout)
                if r.status_code < 400 and r.text and r.text.strip():
                    return r.text
                last_error = f"HTTP {r.status_code}"
            except Exception as exc:
                last_error = str(exc)

            if attempt < len(_RETRY_WAITS):
                label = f" {context}" if context else ""
                print(f"[{_SITE_ID}]{label} curl_cffi attempt {attempt}/3 failed: {last_error}; retrying in {wait}s")
                time.sleep(wait)

        label = f" {context}" if context else ""
        print(f"[{_SITE_ID}]{label} curl_cffi failed after 3 attempts for {url}: {last_error}; falling back to plain curl")
        return None

    def _curl_get(self, url, *, context="", timeout=45):
        """Fetch a URL, preferring curl_cffi (in-process, no subprocess-spawn
        overhead) with a plain-curl subprocess fallback. Not behind a
        WAF/challenge, but subprocess curl calls were prone to slow/stalled
        fetches in this environment; curl_cffi is faster and more reliable.
        """
        if _CFFI_AVAILABLE:
            body = self._cffi_get(url, context=context, timeout=timeout)
            if body:
                return body

        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--connect-timeout",
            "15",
            "--max-time",
            str(timeout),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H",
            "Accept-Language: de-DE,de;q=0.9,en;q=0.8",
            "-H",
            f"Referer: {_START_URL}",
            url,
        ]
        last_error = "unknown error"
        for attempt, wait in enumerate(_RETRY_WAITS, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 10,
                    check=False,
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                last_error = str(exc)
            else:
                body = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and body.strip():
                    return body
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} stderr={stderr}"

            if attempt < len(_RETRY_WAITS):
                label = f" {context}" if context else ""
                print(f"[{_SITE_ID}]{label} curl attempt {attempt}/3 failed: {last_error}; retrying in {wait}s")
                time.sleep(wait)

        label = f" {context}" if context else ""
        print(f"[{_SITE_ID}]{label} curl failed after 3 attempts for {url}: {last_error}")
        return None

    def _make_soup(self, raw, *, context="html"):
        try:
            from bs4 import BeautifulSoup
        except Exception as exc:
            print(f"[{_SITE_ID}] BeautifulSoup unavailable for {context}: {exc}")
            return None

        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                print(f"[{_SITE_ID}] parser {parser} failed for {context}: {exc}")
                continue
        return None

    # ------------------------------------------------------------------
    # List API
    # ------------------------------------------------------------------

    def _list_url(self, offset):
        return f"{_LIST_ENDPOINT}?offset={offset}&limit={_PAGE_SIZE}"

    def _parse_list_page(self, raw, *, page, offset):
        soup = self._make_soup(raw, context=f"list page {page}")
        if soup is None:
            return [], 0

        hits_node = soup.select_one("[data-hits]")
        try:
            hits = int(hits_node.get("data-hits", "0")) if hits_node else 0
        except ValueError:
            hits = 0

        items = []
        for li in soup.select("li[data-js-publication]"):
            try:
                item = self._parse_list_item(li, page=page, offset=offset)
                if item:
                    items.append(item)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] list item parse failed on page {page}: {exc}")
                continue
        return items, hits

    def _parse_list_item(self, li, *, page, offset):
        title = _class_text(li, ".e-publication-teaser__headline")
        abstract = _class_text(li, ".e-publication-teaser__teaser-text")

        pdf_link = li.select_one('a[href*=".pdf"]')
        pdf_url = urljoin(_BASE_URL, pdf_link.get("href", "")) if pdf_link else None
        content_id = None
        order_endpoint = None
        order_number = None
        order = li.select_one("[data-js-order-button]")
        if order:
            content_id = order.get("data-content-id") or None
            order_number = order.get("data-order-number") or None
            if order.get("data-endpoint"):
                order_endpoint = urljoin(_BASE_URL, order.get("data-endpoint"))

        if not content_id:
            content_id = _content_id_from_url(pdf_url)
        if not content_id:
            content_id = _content_id_from_url(order_endpoint)

        date_raw = _definition_value(li, "Sachstandsdatum")
        if not date_raw:
            date_raw = _class_text(li, ".e-publication-teaser__file-info--date")
        listed_date = _parse_date(date_raw)

        file_type = _definition_value(li, "Dateityp") or _class_text(li, ".e-publication-teaser__file-info--file-type")
        file_size = _definition_value(li, "Dateigröße") or _class_text(li, ".e-publication-teaser__file-info--file-size")
        page_count = _definition_value(li, "Seiten") or _class_text(li, ".e-publication-teaser__file-info--page-count")
        accessibility = _definition_value(li, "Zugänglichkeit") or _class_text(li, ".e-publication-teaser__file-info--accessible")

        img = li.select_one("img.e-picture__img[src]")
        image_url = urljoin(_BASE_URL, img.get("src")) if img else None
        image_alt = img.get("alt") if img else None

        if not title or not pdf_url or not content_id:
            return None

        return {
            "title": title,
            "abstract": abstract,
            "pdf_url": pdf_url,
            "content_id": str(content_id),
            "order_number": order_number,
            "order_endpoint": order_endpoint,
            "listed_date": listed_date,
            "date_raw": date_raw,
            "file_type": file_type,
            "file_size": file_size,
            "page_count": page_count,
            "accessibility": accessibility,
            "image_url": image_url,
            "image_alt": image_alt,
            "list_page": page,
            "list_offset": offset,
        }

    # ------------------------------------------------------------------
    # Detail page
    # ------------------------------------------------------------------

    def _parse_detail(self, raw, list_item, detail_url):
        soup = self._make_soup(raw, context=f"detail {detail_url}")
        if soup is None:
            return {}

        title = _class_text(soup, ".m-download-view__headline")
        if not title:
            title = _class_text(soup, "title").replace("| BMZ", "").strip()

        abstract = _class_text(soup, ".m-download-view__description")
        pdf_link = soup.select_one('.m-download-view__anchor[href*=".pdf"], a[href*=".pdf"]')
        pdf_url = urljoin(_BASE_URL, pdf_link.get("href", "")) if pdf_link else None

        date_raw = _definition_value(soup, "Sachstandsdatum")
        published_date = _parse_date(date_raw)

        file_type = _definition_value(soup, "Dateityp")
        file_size = _definition_value(soup, "Dateigröße")
        page_count = _definition_value(soup, "Seiten")
        accessibility = _definition_value(soup, "Zugänglichkeit")

        order = soup.select_one("[data-js-order-button]")
        order_endpoint = None
        order_number = None
        if order:
            order_number = order.get("data-order-number") or None
            if order.get("data-endpoint"):
                order_endpoint = urljoin(_BASE_URL, order.get("data-endpoint"))

        og_title = ""
        og_url = ""
        og_desc = ""
        og_title_el = soup.select_one('meta[property="og:title"]')
        og_url_el = soup.select_one('meta[property="og:url"]')
        og_desc_el = soup.select_one('meta[property="og:description"], meta[name="description"]')
        if og_title_el and og_title_el.get("content"):
            og_title = _clean_text(og_title_el.get("content"))
        if og_url_el and og_url_el.get("content"):
            og_url = _clean_text(og_url_el.get("content"))
        if og_desc_el and og_desc_el.get("content"):
            og_desc = _clean_text(og_desc_el.get("content"))

        return {
            "title": title or og_title,
            "abstract": abstract,
            "pdf_url": pdf_url,
            "date_raw": date_raw,
            "published_date": published_date,
            "file_type": file_type,
            "file_size": file_size,
            "page_count": page_count,
            "accessibility": accessibility,
            "order_number": order_number,
            "order_endpoint": order_endpoint,
            "og_title": og_title,
            "og_url": og_url,
            "og_description": og_desc,
            "detail_url": detail_url,
            "source": "detail",
            # Keep detail discovery evidence small; do not store full HTML.
            "detail_html_chars": len(raw or ""),
        }

    def _build_paper(self, list_item, detail):
        content_id = list_item["content_id"]
        detail_url = _DETAIL_URL_TMPL.format(content_id=content_id)
        pdf_url = detail.get("pdf_url") or list_item.get("pdf_url")
        title = detail.get("title") or list_item.get("title")
        abstract = detail.get("abstract") or list_item.get("abstract")
        listed_date = list_item.get("listed_date")
        published_date = detail.get("published_date") or listed_date
        original_filename = _filename_from_url(pdf_url)
        category = "Publikation"

        metadata = {
            "posted_date": list_item.get("date_raw"),
            "listed_date": listed_date,
            "listed_date_raw": list_item.get("date_raw"),
            "detail_date_raw": detail.get("date_raw"),
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "content_id": content_id,
            "node_id": content_id,
            "post_number": content_id,
            "order_number": detail.get("order_number") or list_item.get("order_number"),
            "order_endpoint": detail.get("order_endpoint") or list_item.get("order_endpoint"),
            "list_endpoint": _LIST_ENDPOINT,
            "start_url": _START_URL,
            "list_page": list_item.get("list_page"),
            "list_offset": list_item.get("list_offset"),
            "file_type": detail.get("file_type") or list_item.get("file_type"),
            "file_size": detail.get("file_size") or list_item.get("file_size"),
            "page_count": detail.get("page_count") or list_item.get("page_count"),
            "accessibility": detail.get("accessibility") or list_item.get("accessibility"),
            "image_url": list_item.get("image_url"),
            "image_alt": list_item.get("image_alt"),
            "og_title": detail.get("og_title"),
            "og_url": detail.get("og_url"),
            "og_description": detail.get("og_description"),
            "list_title": list_item.get("title"),
            "list_abstract": list_item.get("abstract"),
            "detail_html_chars": detail.get("detail_html_chars"),
        }

        return {
            "id": f"{_SITE_ID}:{content_id}",
            "site_id": self.site_id,
            "external_id": content_id,
            "post_number": content_id,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": "",
            "publisher": _PUBLISHER,
            "department": _PUBLISHER,
            "journal": "",
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": "",
            "category": category,
            "doi": "",
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 1
        offset = 0
        seen_urls = set()
        start_time = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

        if limit is not None and limit <= 0:
            print(f"[{_SITE_ID}] done. Total saved: 0")
            return 0

        while True:
            if limit is not None and saved >= limit:
                break
            if page > _MAX_PAGES:
                print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached")
                break

            elapsed = time.time() - start_time
            if elapsed >= _MAX_SECONDS - _STOP_BEFORE_SECONDS:
                print(f"[{_SITE_ID}] approaching 25-minute wall-clock budget; stopping cleanly")
                break

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_or_inf}")

            list_url = self._list_url(offset)
            raw = self._curl_get(list_url, context=f"list page {page}")
            if not raw:
                print(f"[{_SITE_ID}] page {page}: empty response; stopping")
                break

            items, hits = self._parse_list_page(raw, page=page, offset=offset)
            if not items:
                print(f"[{_SITE_ID}] page {page}: 0 records; stopping")
                break

            new_on_page = 0
            for item_index, item in enumerate(items, start=1):
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time >= _MAX_SECONDS - _STOP_BEFORE_SECONDS:
                    print(f"[{_SITE_ID}] approaching 25-minute wall-clock budget during detail loop; stopping cleanly")
                    return saved

                detail_url = _DETAIL_URL_TMPL.format(content_id=item["content_id"])
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                try:
                    time.sleep(self._detail_delay)
                    detail_raw = self._curl_get(detail_url, context=f"item {item['content_id']}")
                    if not detail_raw:
                        raise RuntimeError("detail fetch failed")
                    detail = self._parse_detail(detail_raw, item, detail_url)
                    paper = self._build_paper(item, detail)

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < _MIN_ABSTRACT_CHARS:
                        print(
                            f"[{_SITE_ID}] item {item['content_id']} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] saved {saved}/{limit_or_inf}: {paper['title'][:80]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {item.get('content_id') or item_index} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{_SITE_ID}] page {page}: 0 new records; stopping")
                break

            # The live component advances by the number of returned items.
            offset += len(items)
            page += 1

            if hits and offset >= hits:
                break

        print(f"[{_SITE_ID}] done. Total saved: {saved}")
        return saved
