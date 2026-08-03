# -*- coding: utf-8 -*-
"""Crawler for Klimarådet Danish publications.

Discovery:
  Starting URL: https://klimaraadet.dk/da/udgivelser
  The site is Drupal 10. The publication list is rendered server-side by the
  Drupal Views endpoint `/da/udgivelser?page=N`, where N is zero-based. Detail
  pages expose metadata in the page HTML and the native node id in Drupal
  settings / language-switcher attributes (`node/NNN`).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime
from html import unescape
from urllib.parse import urljoin, urlparse, unquote

from crawler.base_crawler import BaseCrawler


_SITE_ID = "klimaraadet-dk-da"
_BASE_URL = "https://klimaraadet.dk"
_LIST_URL = f"{_BASE_URL}/da/udgivelser"
_PUBLISHER = "Klimarådet"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_ABSTRACT_MIN_LEN = 100
_ABSTRACT_MAX_LEN = 6000

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_DA_MONTHS = {
    "januar": 1,
    "februar": 2,
    "marts": 3,
    "april": 4,
    "maj": 5,
    "juni": 6,
    "juli": 7,
    "august": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "december": 12,
}


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = unescape(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib -> lxml -> html.parser."""
    from bs4 import BeautifulSoup

    last_exc = None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception as exc:
            last_exc = exc
            continue
    raise RuntimeError(f"No HTML parser available: {last_exc}")


def _curl_get(url: str, *, timeout: int = 35, retries: int = 3) -> str | None:
    """Fetch a URL using curl with TLS max 1.3 and exponential backoff."""
    cmd = [
        "curl",
        "--tls-max",
        "1.3",
        "-skL",
        "--max-time",
        str(timeout),
        "-H",
        f"User-Agent: {_USER_AGENT}",
        "-H",
        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
        "-H",
        "Accept-Language: da-DK,da;q=0.9,en-US;q=0.8,en;q=0.7",
        url,
    ]
    waits = (1, 3, 9)
    for attempt in range(retries):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout + 5,
                check=False,
            )
            raw = result.stdout.decode("utf-8", errors="replace")
            if result.returncode == 0 and raw.strip():
                return raw
            err = result.stderr.decode("utf-8", errors="replace").strip()
            msg = err or f"empty response/returncode={result.returncode}"
            raise RuntimeError(msg)
        except Exception as exc:
            if attempt >= retries - 1:
                print(f"[{_SITE_ID}] fetch failed after {retries} attempts for {url}: {exc}")
                return None
            wait = waits[min(attempt, len(waits) - 1)]
            print(
                f"[{_SITE_ID}] fetch error for {url}: {exc}; "
                f"retry {attempt + 1}/{retries} in {wait}s"
            )
            time.sleep(wait)
    return None


def _absolute_url(href: str | None) -> str | None:
    if not href:
        return None
    return urljoin(_BASE_URL, href.strip())


def _iso_date(value: str | None) -> str | None:
    if not value:
        return None
    value = _clean_text(value)

    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"

    match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", value)
    if match:
        day, month, year = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        return f"{year:04d}-{month:02d}-{day:02d}"

    match = re.search(
        r"(?:den\s+)?(\d{1,2})\.\s+([a-zæøå]+)\s+(\d{4})",
        value,
        re.IGNORECASE,
    )
    if match:
        day = int(match.group(1))
        month = _DA_MONTHS.get(match.group(2).lower())
        year = int(match.group(3))
        if month:
            return f"{year:04d}-{month:02d}-{day:02d}"

    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urlparse(url).path
    filename = unquote(path.rstrip("/").split("/")[-1])
    if filename and len(filename) <= 240:
        return filename
    return None


def _slug_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urlparse(url).path.strip("/")
    if not path:
        return None
    return path.split("/")[-1] or None


def _script_text(el) -> str:
    if not el:
        return ""
    if el.string:
        return el.string.strip()
    return "".join(str(part) for part in el.contents).strip()


def _node_id_from_soup(soup) -> str | None:
    settings = soup.select_one('script[type="application/json"][data-drupal-selector="drupal-settings-json"]')
    if settings:
        try:
            data = json.loads(_script_text(settings))
            current_path = data.get("path", {}).get("currentPath", "")
            match = re.search(r"node/(\d+)", current_path)
            if match:
                return match.group(1)
        except Exception:
            pass

    for el in soup.select(
        'a.language-link[data-drupal-link-system-path^="node/"], '
        'ul.links [data-drupal-link-system-path^="node/"]'
    ):
        value = el.get("data-drupal-link-system-path", "")
        match = re.search(r"node/(\d+)", value)
        if match:
            return match.group(1)
    return None


def _node_type_from_class(el) -> str | None:
    if not el:
        return None
    for cls in el.get("class", []):
        match = re.match(r"node--type-(.+)", cls)
        if match:
            return match.group(1)
    return None


def _text_from_first(el, selectors: list[str]) -> str:
    if not el:
        return ""
    for selector in selectors:
        match = el.select_one(selector)
        if match:
            text = _clean_text(match.get_text(" ", strip=True))
            if text:
                return text
    return ""


def _category_from_time_text(text: str) -> str | None:
    text = _clean_text(text)
    if not text:
        return None
    match = re.match(r"([^—–-]{2,60})\s*[—–-]", text)
    if match:
        return _clean_text(match.group(1))
    return None


def _parse_list_items(html: str) -> tuple[list[dict], bool]:
    try:
        soup = _make_soup(html)
    except Exception as exc:
        print(f"[{_SITE_ID}] list parse failed: {exc}")
        return [], False

    view = soup.select_one(".view-publications.view-display-id-block_main")
    if not view:
        view = soup.select_one(".view-publications")
    if not view:
        return [], False

    content = view.select_one(".view-content")
    if not content:
        return [], False

    rows = content.find_all("div", class_="views-row", recursive=False)
    items = []
    for row in rows:
        teaser = row.select_one("a.base-teaser-styling[href]")
        if teaser is None:
            teaser = row.select_one(".base-teaser-styling a[href]")
        if teaser is None:
            teaser = row.select_one("a[href]")
        if teaser is None:
            continue

        href = teaser.get("href", "").strip()
        url = _absolute_url(href)
        if not url:
            continue

        title = _text_from_first(
            row,
            [
                ".field--name-title",
                "h3",
                "a",
            ],
        )
        time_el = row.select_one("time[datetime]")
        listed_datetime = time_el.get("datetime", "").strip() if time_el else ""
        date_raw = _clean_text(time_el.get_text(" ", strip=True)) if time_el else ""
        listed_date = _iso_date(listed_datetime) or _iso_date(date_raw)

        category = _text_from_first(row, [".field--name-field-publication-type"])
        if not category:
            category = _category_from_time_text(date_raw)

        node_el = row.select_one(".base-teaser-styling")
        node_type = _node_type_from_class(node_el) or _node_type_from_class(teaser)

        items.append(
            {
                "url": url,
                "title": title,
                "listed_datetime": listed_datetime,
                "listed_date": listed_date,
                "date_raw": date_raw,
                "category": category,
                "node_type": node_type,
                "slug": _slug_from_url(url),
            }
        )

    has_next = bool(view.select_one(".pager__item--next a[href], a[rel='next']"))
    return items, has_next


def _extract_pdf_links(scope) -> list[dict]:
    links = []
    for a in scope.select("a[href]"):
        href = a.get("href", "").strip()
        if ".pdf" not in href.lower():
            continue
        url = _absolute_url(href)
        if not url:
            continue
        links.append(
            {
                "url": url,
                "title": _clean_text(a.get("title")) or _clean_text(a.get_text(" ", strip=True)),
                "filename": _filename_from_url(url),
            }
        )
    return links


def _append_part(parts: list[str], text: str) -> None:
    text = _clean_text(text)
    if not text:
        return
    if text in parts:
        return
    current_len = sum(len(p) for p in parts)
    if current_len >= _ABSTRACT_MAX_LEN:
        return
    remaining = _ABSTRACT_MAX_LEN - current_len
    if len(text) > remaining:
        text = text[:remaining].rsplit(" ", 1)[0].strip()
    if text:
        parts.append(text)


def _parse_detail_page(html: str, url: str) -> dict:
    soup = _make_soup(html)
    article = soup.select_one("article.node--view-mode-pageview")
    if article is None:
        article = soup.select_one("article.node--view-mode-full") or soup

    node_id = _node_id_from_soup(soup)
    title = _text_from_first(article, ["h1 .field--name-title", "h1"])

    category = _text_from_first(article, [".field--name-field-publication-type"])
    label_el = article.select_one(".node__content--main > .label, .node__content--main .label")
    label_raw = _clean_text(label_el.get_text(" ", strip=True)) if label_el else ""
    if not category and label_raw:
        category = re.split(r"—|–|-|Offentliggjort", label_raw, maxsplit=1)[0].strip() or None

    published_date = _iso_date(label_raw)

    meta_description = ""
    og_description = ""
    meta = soup.select_one('meta[name="description"]')
    if meta:
        meta_description = _clean_text(meta.get("content"))
    og = soup.select_one('meta[property="og:description"]')
    if og:
        og_description = _clean_text(og.get("content"))

    parts: list[str] = []
    manchet = article.select_one(".field--name-field-manchet")
    if manchet:
        _append_part(parts, manchet.get_text(" ", strip=True))

    # Add real body text so report pages with short subtitles still have a
    # useful abstract. Limit size to keep DB rows reasonable.
    body_selectors = [
        ".node__content--main .field--name-body",
        ".field--name-field-body",
        ".field--name-field-text-formatted-long",
    ]
    for selector in body_selectors:
        for body in article.select(selector):
            if "field--name-field-members" in body.get("class", []):
                continue
            _append_part(parts, body.get_text(" ", strip=True))
            if sum(len(p) for p in parts) >= 1200:
                break
        if sum(len(p) for p in parts) >= 1200:
            break

    if not parts and og_description:
        _append_part(parts, og_description)
    if not parts and meta_description:
        _append_part(parts, meta_description)
    if len(" ".join(parts)) < _ABSTRACT_MIN_LEN and og_description:
        _append_part(parts, og_description)
    if len(" ".join(parts)) < _ABSTRACT_MIN_LEN and meta_description:
        _append_part(parts, meta_description)

    abstract = "\n\n".join(parts).strip()
    pdf_links = _extract_pdf_links(article)
    primary_pdf = pdf_links[0]["url"] if pdf_links else None
    original_filename = pdf_links[0]["filename"] if pdf_links else None

    current_path = None
    settings = soup.select_one('script[type="application/json"][data-drupal-selector="drupal-settings-json"]')
    if settings:
        try:
            data = json.loads(_script_text(settings))
            current_path = data.get("path", {}).get("currentPath")
        except Exception:
            current_path = None

    return {
        "url": url,
        "node_id": node_id,
        "current_path": current_path,
        "title": title,
        "category": category,
        "date_raw": label_raw,
        "published_date": published_date,
        "abstract": abstract,
        "meta_description": meta_description,
        "og_description": og_description,
        "pdf_url": primary_pdf,
        "pdf_links": pdf_links,
        "original_filename": original_filename,
        "node_type": _node_type_from_class(article),
    }


class KlimaraadetDkDaCrawler(BaseCrawler):
    site_id = "klimaraadet-dk-da"
    site_name = "Custom: klimaraadet-dk-da"
    base_url = "https://klimaraadet.dk"

    def crawl(self, limit=None):
        saved = 0
        page = 0
        pages_scanned = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_or_inf = limit if limit is not None else "inf"

        if limit is not None and limit <= 0:
            return 0

        while True:
            if page >= _MAX_PAGES:
                print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached; stopping")
                break
            if time.time() - start_time >= _MAX_WALL_SECONDS:
                print(f"[{_SITE_ID}] approaching 25 minute wall-clock budget; stopping cleanly")
                break
            if limit is not None and saved >= limit:
                break

            list_url = _LIST_URL if page == 0 else f"{_LIST_URL}?page={page}"
            raw = _curl_get(list_url)
            if not raw:
                print(f"[{_SITE_ID}] page {page + 1}: list fetch failed; stopping")
                break

            items, has_next = _parse_list_items(raw)
            pages_scanned += 1
            if pages_scanned % 10 == 0:
                print(f"[{_SITE_ID}] page {pages_scanned}: saved {saved}/{limit_or_inf}")

            if not items:
                print(f"[{_SITE_ID}] page {page + 1}: no records; stopping")
                break

            new_items = []
            for item in items:
                item_url = item.get("url")
                if not item_url or item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                new_items.append(item)

            if not new_items:
                print(f"[{_SITE_ID}] page {page + 1}: 0 new records; stopping")
                break

            stop_for_time = False
            for item in new_items:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time >= _MAX_WALL_SECONDS:
                    print(f"[{_SITE_ID}] approaching 25 minute wall-clock budget; stopping cleanly")
                    stop_for_time = True
                    break

                item_url = item.get("url")
                try:
                    time.sleep(getattr(self, "_delay", 1.0))
                    detail_raw = _curl_get(item_url)
                    if not detail_raw:
                        print(f"[{_SITE_ID}] item {item_url} failed: detail fetch returned empty")
                        continue

                    detail = _parse_detail_page(detail_raw, item_url)
                    title = detail.get("title") or item.get("title") or "(untitled)"
                    abstract = detail.get("abstract") or ""
                    if len(abstract) < _ABSTRACT_MIN_LEN:
                        print(
                            f"[{_SITE_ID}] item {item_url} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    node_id = detail.get("node_id")
                    slug = item.get("slug") or _slug_from_url(item_url)
                    external_id = node_id or slug or item_url
                    post_number = node_id or slug
                    listed_date = item.get("listed_date")
                    published_date = detail.get("published_date") or listed_date
                    category = detail.get("category") or item.get("category")
                    pdf_url = detail.get("pdf_url")
                    original_filename = detail.get("original_filename") or _filename_from_url(pdf_url)

                    metadata = {
                        "posted_date": item.get("date_raw") or item.get("listed_datetime") or listed_date,
                        "listed_date": listed_date,
                        "listed_datetime": item.get("listed_datetime"),
                        "originalFilename": original_filename,
                        "journal_raw": None,
                        "series": None,
                        "volume": None,
                        "issue": None,
                        "node_id": node_id,
                        "current_path": detail.get("current_path"),
                        "post_number": post_number,
                        "slug": slug,
                        "category": category,
                        "node_type": detail.get("node_type") or item.get("node_type"),
                        "list_page": page,
                        "list_endpoint": list_url,
                        "detail_url": item_url,
                        "detail_date_raw": detail.get("date_raw"),
                        "list_item": item,
                        "pdf_links": detail.get("pdf_links") or [],
                        "meta_description": detail.get("meta_description"),
                        "og_description": detail.get("og_description"),
                        "source_system": "Drupal 10 HTML view",
                    }

                    paper = {
                        "id": f"{self.site_id}:{external_id}",
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
                        "department": None,
                        "journal": None,
                        "url": item_url,
                        "pdf_url": pdf_url,
                        "keywords": None,
                        "category": category,
                        "doi": None,
                        "original_filename": original_filename,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }
                    self._save_paper(paper)
                    saved += 1
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {item_url} failed: {exc}")
                    continue

            if stop_for_time:
                break
            if not has_next:
                break
            page += 1

        return saved
