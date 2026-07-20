# -*- coding: utf-8 -*-
"""Crawler for KommuneKredit reports.

Discovery:
  https://www.kommunekredit.com/about-us/reports-numbers-and-publications/reports/
  returns server-rendered HTML from ASP.NET/IIS. The report lists are embedded
  as <script type="application/json"> blocks with moduleName="DownloadList".
  The live page exposes no separate paged list API and no per-report HTML
  detail page; each item links directly to a PDF under /media/.

Item detail:
  For each PDF item, curl fetches response headers to capture Last-Modified,
  ETag, Content-Length, Content-Type and any Content-Disposition filename.
"""

import json
import re
import subprocess
import sys
import time
import uuid
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import unquote, urljoin, urlparse

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler


_SITE_ID = "kommunekredit-com-about-us"
_BASE_URL = "https://www.kommunekredit.com"
_LIST_URL = (
    "https://www.kommunekredit.com/about-us/"
    "reports-numbers-and-publications/reports/"
)
_PUBLISHER = "KommuneKredit"
_MAX_PAGES = 200
_MAX_WALL_SECONDS = 25 * 60

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _make_soup(raw: str):
    """Parse HTML with fallback chain: html5lib -> lxml -> html.parser."""
    from bs4 import BeautifulSoup

    last_exc = None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception as exc:
            last_exc = exc
            continue
    raise RuntimeError(f"no usable HTML parser: {last_exc}")


def _curl_text(url: str, *, timeout: int = 45, retries: int = 3) -> str | None:
    """Fetch text with curl, TLS max 1.3, and exponential backoff."""
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
        "Accept: text/html,application/xhtml+xml,application/json,*/*;q=0.8",
        "-H",
        "Accept-Language: en-US,en;q=0.9,da;q=0.7",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            raw = result.stdout.decode("utf-8", errors="replace")
            if result.returncode == 0 and raw.strip():
                return raw
            message = raw[:200].strip() or result.stderr.decode("utf-8", errors="replace")[:200].strip()
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(
                    f"[{_SITE_ID}] fetch failed for {url}: {message}; "
                    f"retry {attempt + 1}/{retries} in {wait}s"
                )
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(
                    f"[{_SITE_ID}] fetch error for {url}: {exc}; "
                    f"retry {attempt + 1}/{retries} in {wait}s"
                )
                time.sleep(wait)
            else:
                print(f"[{_SITE_ID}] fetch failed after {retries} attempts for {url}: {exc}")
    return None


def _curl_headers(url: str, *, timeout: int = 45, retries: int = 3) -> str | None:
    """Fetch response headers for a URL with curl and retry backoff."""
    cmd = [
        "curl",
        "--tls-max",
        "1.3",
        "-skL",
        "-I",
        "--max-time",
        str(timeout),
        "-H",
        f"User-Agent: {_USER_AGENT}",
        "-H",
        "Accept: application/pdf,*/*;q=0.8",
        "-H",
        f"Referer: {_LIST_URL}",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            raw = result.stdout.decode("utf-8", errors="replace")
            if result.returncode == 0 and "HTTP/" in raw:
                return raw
            message = raw[:200].strip() or result.stderr.decode("utf-8", errors="replace")[:200].strip()
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(
                    f"[{_SITE_ID}] header fetch failed for {url}: {message}; "
                    f"retry {attempt + 1}/{retries} in {wait}s"
                )
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(
                    f"[{_SITE_ID}] header fetch error for {url}: {exc}; "
                    f"retry {attempt + 1}/{retries} in {wait}s"
                )
                time.sleep(wait)
            else:
                print(f"[{_SITE_ID}] header fetch failed after {retries} attempts for {url}: {exc}")
    return None


def _parse_header_map(raw_headers: str | None) -> dict:
    """Parse curl -I output into a lowercase-key header map."""
    headers = {}
    if not raw_headers:
        return headers
    for line in raw_headers.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key:
            headers[key] = value
    return headers


def _iso_date_from_http_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
        return dt.date().isoformat()
    except Exception:
        return None


def _post_number_from_http_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y%m%d%H%M%S")
    except Exception:
        return None


def _title_year(title: str) -> str | None:
    match = re.search(r"\b(20\d{2}|19\d{2})\b", title or "")
    return match.group(1) if match else None


def _fallback_published_date(title: str) -> str | None:
    year = _title_year(title)
    if not year:
        return None
    if re.search(r"\b(first half|h1|interim)\b", title, re.I):
        return f"{year}-06-30"
    return f"{year}-12-31"


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _absolute_url(href: str | None) -> str | None:
    if not href:
        return None
    return urljoin(_BASE_URL, href.strip())


def _filename_from_content_disposition(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', value, re.I)
    if not match:
        return None
    filename = unquote(match.group(1).strip())
    return filename or None


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urlparse(url).path
    filename = unquote(path.rstrip("/").split("/")[-1])
    return filename or None


def _media_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) >= 3 and parts[0] == "media":
        return parts[1]
    return None


def _external_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) >= 3 and parts[0] == "media":
        return "/".join(parts[1:])
    return urlparse(url).path.strip("/") or url


def _category_from_module_title(title: str | None) -> str | None:
    title = _clean_text(title)
    title = re.sub(r"^Download\s+", "", title, flags=re.I)
    title = re.sub(r"\s*\(.*?\)\s*", " ", title).strip()
    return title or None


def _keywords_for(category: str | None, title: str) -> str:
    words = ["KommuneKredit", "reports"]
    if category:
        words.append(category)
    year = _title_year(title)
    if year:
        words.append(year)
    return ", ".join(dict.fromkeys(words))


def _build_abstract(title: str, category: str | None, hero_text: str, features: list[str]) -> str:
    feature_text = ", ".join(_clean_text(f) for f in features if _clean_text(f))
    parts = [
        f"{title} is a KommuneKredit report listed in the {category or 'Reports'} section.",
        hero_text,
    ]
    if feature_text:
        parts.append(f"The source page lists the PDF file metadata as: {feature_text}.")
    abstract = " ".join(p for p in parts if p)
    return _clean_text(abstract)


def _extract_json_modules(html: str) -> tuple[list[dict], dict]:
    try:
        soup = _make_soup(html)
    except Exception as exc:
        print(f"[{_SITE_ID}] list HTML parse failed: {exc}")
        return [], {}

    modules = []
    site_config = {}
    for script in soup.find_all("script", attrs={"type": "application/json"}):
        raw = script.string if script.string is not None else script.get_text()
        raw = raw.strip() if raw else ""
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception as exc:
            print(f"[{_SITE_ID}] JSON module parse failed: {exc}")
            continue
        if isinstance(data, dict):
            if data.get("moduleName"):
                modules.append(data)
            elif "apiOptions" in data:
                site_config = data
    return modules, site_config


def _extract_x_page_id(site_config: dict) -> str | None:
    headers = ((site_config or {}).get("apiOptions") or {}).get("headers") or []
    for item in headers:
        if str(item.get("key", "")).lower() == "x-page-id":
            value = item.get("value")
            return str(value) if value is not None else None
    return None


def _parse_download_items(html: str, page_url: str) -> tuple[list[dict], bool, dict]:
    """Return item dicts, has_next_page, and page-level metadata."""
    modules, site_config = _extract_json_modules(html)
    hero_text = ""
    for module in modules:
        if module.get("moduleName") == "SubpageHero":
            hero_text = _clean_text(module.get("text"))
            break

    page_meta = {
        "discovered_endpoint": page_url,
        "discovered_detail_endpoint": "PDF media URL headers",
        "x_page_id": _extract_x_page_id(site_config),
        "language": site_config.get("language"),
        "theme": site_config.get("theme"),
        "hero_text": hero_text,
    }

    items = []
    for module_index, module in enumerate(modules):
        if module.get("moduleName") != "DownloadList":
            continue
        category = _category_from_module_title(module.get("title"))
        anchor = module.get("anchor")
        for column_index, column in enumerate(module.get("columns") or []):
            for file_index, file_item in enumerate(column.get("files") or []):
                link = file_item.get("link") or {}
                title = _clean_text(link.get("label"))
                pdf_url = _absolute_url(link.get("href"))
                if not title or not pdf_url:
                    continue
                features = file_item.get("features") or []
                items.append(
                    {
                        "title": title,
                        "category": category,
                        "anchor": anchor,
                        "url": f"{page_url}#{anchor}" if anchor else page_url,
                        "pdf_url": pdf_url,
                        "features": features,
                        "abstract": _build_abstract(title, category, hero_text, features),
                        "raw_file": file_item,
                        "raw_module": {
                            "moduleName": module.get("moduleName"),
                            "title": module.get("title"),
                            "anchor": anchor,
                            "theme": module.get("theme"),
                            "module_index": module_index,
                            "column_index": column_index,
                            "file_index": file_index,
                        },
                    }
                )

    has_next = False
    try:
        soup = _make_soup(html)
        for link in soup.find_all("a", href=True):
            rel = " ".join(link.get("rel") or []).lower()
            text = _clean_text(link.get_text(" "))
            href = link.get("href", "")
            if "next" in rel or text.lower() in {"next", "next page"}:
                has_next = True
                break
            if re.search(r"[?&](page|pageNumber)=\d+", href, re.I):
                has_next = True
    except Exception as exc:
        print(f"[{_SITE_ID}] pagination parse failed: {exc}")

    return items, has_next, page_meta


def _enrich_with_pdf_headers(item: dict) -> dict:
    raw_headers = _curl_headers(item["pdf_url"])
    headers = _parse_header_map(raw_headers)
    last_modified_raw = headers.get("last-modified")
    listed_date = _iso_date_from_http_date(last_modified_raw)
    published_date = listed_date or _fallback_published_date(item["title"])
    original_filename = (
        _filename_from_content_disposition(headers.get("content-disposition"))
        or _filename_from_url(item["pdf_url"])
    )
    post_number = (
        _post_number_from_http_date(last_modified_raw)
        or _title_year(item["title"])
        or _media_id_from_url(item["pdf_url"])
    )

    enriched = dict(item)
    enriched.update(
        {
            "headers": headers,
            "raw_headers": raw_headers,
            "listed_date": listed_date,
            "published_date": published_date,
            "posted_date_raw": last_modified_raw,
            "post_number": post_number,
            "media_id": _media_id_from_url(item["pdf_url"]),
            "external_id": _external_id_from_url(item["pdf_url"]),
            "original_filename": original_filename,
        }
    )
    return enriched


def _paper_from_item(item: dict, page_meta: dict) -> dict:
    external_id = item.get("external_id") or _external_id_from_url(item.get("pdf_url"))
    metadata = {
        "posted_date": item.get("posted_date_raw") or item.get("listed_date"),
        "listed_date": item.get("listed_date"),
        "originalFilename": item.get("original_filename"),
        "journal_raw": None,
        "series": None,
        "volume": None,
        "issue": None,
        "node_id": item.get("media_id"),
        "media_id": item.get("media_id"),
        "post_number": item.get("post_number"),
        "anchor": item.get("anchor"),
        "features": item.get("features") or [],
        "headers": item.get("headers") or {},
        "raw_headers": item.get("raw_headers"),
        "raw_file": item.get("raw_file") or {},
        "raw_module": item.get("raw_module") or {},
        "page": page_meta,
    }
    return {
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{_SITE_ID}:{external_id}")),
        "site_id": _SITE_ID,
        "external_id": external_id,
        "post_number": item.get("post_number"),
        "title": item.get("title"),
        "abstract": item.get("abstract"),
        "published_date": item.get("published_date"),
        "listed_date": item.get("listed_date"),
        "posted_date": item.get("listed_date"),
        "authors": None,
        "publisher": _PUBLISHER,
        "department": None,
        "journal": None,
        "url": item.get("url"),
        "pdf_url": item.get("pdf_url"),
        "keywords": _keywords_for(item.get("category"), item.get("title") or ""),
        "category": item.get("category"),
        "doi": None,
        "original_filename": item.get("original_filename"),
        "metadata": json.dumps(metadata, ensure_ascii=False),
    }


class KommunekreditComAboutUsCrawler(BaseCrawler):
    site_id = "kommunekredit-com-about-us"
    site_name = "Custom: kommunekredit-com-about-us"
    base_url = "https://www.kommunekredit.com"

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls = set()
        start_time = time.time()
        limit_or_inf = limit if limit is not None else "inf"

        while True:
            if limit is not None and saved >= limit:
                break
            if page > _MAX_PAGES:
                print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached")
                break
            if time.time() - start_time >= _MAX_WALL_SECONDS - 30:
                print(f"[{_SITE_ID}] approaching 25 minute wall-clock budget; exiting cleanly")
                break
            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_or_inf}")

            list_url = _LIST_URL if page == 1 else f"{_LIST_URL}?page={page}"
            raw = _curl_text(list_url)
            if not raw:
                print(f"[{_SITE_ID}] page {page} returned no usable response")
                break

            items, has_next, page_meta = _parse_download_items(raw, list_url)
            if not items:
                print(f"[{_SITE_ID}] page {page} returned 0 records")
                break

            new_candidates = 0
            for idx, item in enumerate(items, 1):
                dedupe_url = item.get("pdf_url") or item.get("url")
                if not dedupe_url:
                    continue
                if dedupe_url in seen_urls:
                    print(f"[{_SITE_ID}] duplicate skipped: {dedupe_url}")
                    continue
                seen_urls.add(dedupe_url)
                new_candidates += 1

                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time >= _MAX_WALL_SECONDS - 30:
                    print(f"[{_SITE_ID}] approaching 25 minute wall-clock budget; exiting cleanly")
                    return saved

                try:
                    time.sleep(self._delay)
                    detail = _enrich_with_pdf_headers(item)
                    abstract = _clean_text(detail.get("abstract"))
                    if len(abstract) < 50:
                        print(f"[{_SITE_ID}] short abstract skipped: {detail.get('title')}")
                        continue

                    paper = _paper_from_item(detail, page_meta)
                    self._save_paper(paper)
                    saved += 1
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {idx} failed: {exc}")
                    continue

            if new_candidates == 0:
                print(f"[{_SITE_ID}] page {page} had 0 new records")
                break
            if not has_next:
                break
            page += 1

        return saved
