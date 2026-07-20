# -*- coding: utf-8 -*-
"""Crawler for geoportal.statistics.gov.uk document search.

The portal is an ArcGIS Hub site. Its UI route:

    /search?collection=document&sort=Date%20Updated%7Cmodified%7Cdesc

is backed by the public OGC search API:

    /api/search/v1/collections/document/items?sortBy=-properties.modified

Individual records are exposed at:

    /api/search/v1/collections/document/items/{item_id}
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timezone
from html import unescape
from urllib.parse import unquote, urlencode, urlparse

from crawler.base_crawler import BaseCrawler


_SITE_ID = "geoportal-statistics-gov-uk-search"
_BASE_URL = "https://geoportal.statistics.gov.uk"
_API_ITEMS = f"{_BASE_URL}/api/search/v1/collections/document/items"
_ARCGIS_DATA_URL = "https://www.arcgis.com/sharing/rest/content/items/{item_id}/data"
_PAGE_SIZE = 50
_SAFETY_CAP_PAGES = 200
_WALL_CLOCK_BUDGET_SECONDS = 25 * 60
_MIN_ABSTRACT_CHARS = 100


def _clean_space(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_html(raw: object) -> str:
    """Convert malformed ArcGIS HTML descriptions to readable text.

    BeautifulSoup construction is intentionally guarded and parser order follows
    the site-crawler robustness rule: html5lib -> lxml -> html.parser.
    """
    if raw is None:
        return ""

    html_text = str(raw)
    if not html_text.strip():
        return ""

    try:
        from bs4 import BeautifulSoup
    except Exception:
        text = re.sub(r"<br\s*/?>", "\n", html_text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        return _clean_space(unescape(text))

    for parser_name in ("html5lib", "lxml", "html.parser"):
        try:
            soup = BeautifulSoup(html_text, parser_name)
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            return _clean_space(soup.get_text(" "))
        except Exception:
            continue

    text = re.sub(r"<br\s*/?>", "\n", html_text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return _clean_space(unescape(text))


def _ms_to_date(value: object) -> str | None:
    if value in (None, ""):
        return None
    try:
        numeric = int(float(str(value)))
        # ArcGIS item dates are epoch milliseconds; tolerate epoch seconds too.
        if numeric > 10_000_000_000:
            numeric = numeric // 1000
        return datetime.fromtimestamp(numeric, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        text = str(value).strip()
        match = re.search(r"(20\d{2}|19\d{2})[-/](\d{1,2})[-/](\d{1,2})", text)
        if match:
            year, month, day = match.groups()
            try:
                return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
            except Exception:
                return None
        return None


def _join(items: object, sep: str) -> str | None:
    if not items:
        return None
    if isinstance(items, (list, tuple, set)):
        values = [str(item).strip() for item in items if str(item).strip()]
    else:
        values = [str(items).strip()]
    return sep.join(values) if values else None


def _category_string(categories: object) -> str | None:
    if not isinstance(categories, list):
        return None
    cleaned = []
    for category in categories:
        if not category:
            continue
        text = str(category).strip().strip("/")
        if text.lower().startswith("categories/"):
            text = text[len("categories/") :]
        text = text.replace("/", " > ")
        if text:
            cleaned.append(text)
    return ", ".join(cleaned) if cleaned else None


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urlparse(url).path.rstrip("/")
    if not path:
        return None
    tail = unquote(path.rsplit("/", 1)[-1])
    if tail and "." in tail and len(tail) <= 240:
        return tail
    return None


def _filename_from_content_disposition(header_text: str) -> str | None:
    match = re.search(r"filename\*=UTF-8''([^;\r\n]+)", header_text, flags=re.IGNORECASE)
    if match:
        return unquote(match.group(1).strip().strip('"'))
    match = re.search(r'filename="([^"\r\n]+)"', header_text, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()
    match = re.search(r"filename=([^;\r\n]+)", header_text, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip().strip('"')
    return None


def _is_pdf(props: dict) -> bool:
    item_type = str(props.get("type") or "").lower()
    name = str(props.get("name") or "").lower()
    type_keywords = [str(value).lower() for value in (props.get("typeKeywords") or [])]
    return item_type == "pdf" or name.endswith(".pdf") or "pdf" in type_keywords


def _build_abstract(props: dict) -> str:
    description = _strip_html(props.get("description"))
    snippet = _strip_html(props.get("snippet"))

    parts = []
    if description:
        parts.append(description)
    if snippet and snippet.lower() not in description.lower():
        parts.append(snippet)

    abstract = _clean_space(" ".join(parts))
    if len(abstract) >= _MIN_ABSTRACT_CHARS:
        return abstract
    if len(abstract) < 50:
        return abstract

    supplements = []
    item_type = props.get("type")
    source = props.get("source") or props.get("accessInformation")
    name = props.get("name")
    tags = props.get("tags") or []
    if item_type:
        supplements.append(f"Item type: {item_type}.")
    if source:
        supplements.append(f"Source: {source}.")
    if name:
        supplements.append(f"Original item filename: {name}.")
    if tags:
        supplements.append("Tags: " + ", ".join(str(tag) for tag in tags[:12]) + ".")

    if supplements:
        abstract = _clean_space(" ".join([abstract] + supplements))
    return abstract


class GeoportalStatisticsGovUkSearchCrawler(BaseCrawler):
    site_id = "geoportal-statistics-gov-uk-search"
    site_name = "Custom: geoportal-statistics-gov-uk-search"
    base_url = "https://geoportal.statistics.gov.uk"

    def crawl(self, limit=None):
        saved = 0
        page = 0
        start_time = time.time()
        seen_urls: set[str] = set()
        seen_page_urls: set[str] = set()
        limit_or_inf = limit if limit is not None else "inf"

        params = {
            "limit": _PAGE_SIZE,
            "sortBy": "-properties.modified",
        }
        next_url = f"{_API_ITEMS}?{urlencode(params)}"

        while next_url and page < _SAFETY_CAP_PAGES:
            if time.time() - start_time >= _WALL_CLOCK_BUDGET_SECONDS:
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached, exiting cleanly.")
                break

            if limit is not None and saved >= limit:
                break

            if next_url in seen_page_urls:
                print(f"[{_SITE_ID}] pagination loop detected at page URL, stopping.")
                break
            seen_page_urls.add(next_url)

            page += 1
            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_or_inf}")

            data = self._fetch_json(next_url, context=f"page {page}")
            if not isinstance(data, dict):
                print(f"[{_SITE_ID}] page {page}: failed to parse list response, stopping.")
                break

            features = data.get("features") or []
            if not features:
                print(f"[{_SITE_ID}] page {page}: no records, end of pagination.")
                break

            new_urls_on_page = 0
            for feature in features:
                if limit is not None and saved >= limit:
                    break

                item_id = "?"
                detail_url = None
                try:
                    if not isinstance(feature, dict):
                        continue
                    item_id = str(feature.get("id") or "").strip()
                    if not item_id:
                        print(f"[{_SITE_ID}] item ?: missing native id, skipping.")
                        continue

                    detail_page_url = self._detail_page_url(item_id)
                    if detail_page_url in seen_urls:
                        continue
                    seen_urls.add(detail_page_url)
                    new_urls_on_page += 1

                    detail_url = self._detail_api_url(item_id)
                    detail = self._fetch_json(detail_url, context=f"item {item_id}")
                    if not isinstance(detail, dict):
                        print(f"[{_SITE_ID}] item {item_id}: detail fetch failed, skipping.")
                        continue

                    paper = self._paper_from_detail(detail, feature)
                    if not paper:
                        continue

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {item_id or '?'} failed: {exc}")
                    continue
                finally:
                    if limit is None or saved < limit:
                        delay = getattr(self, "_delay", 1.0)
                        if delay and delay > 0:
                            time.sleep(delay)

            if new_urls_on_page == 0:
                print(f"[{_SITE_ID}] page {page}: no unseen records, stopping.")
                break

            next_url = self._next_link(data)
            if not next_url:
                print(f"[{_SITE_ID}] page {page}: no next link, end of pagination.")
                break

        if page >= _SAFETY_CAP_PAGES:
            print(f"[{_SITE_ID}] Safety cap of {_SAFETY_CAP_PAGES} pages reached.")

        print(f"[{_SITE_ID}] Done. Saved {saved} items.")
        return saved

    def _paper_from_detail(self, detail: dict, list_feature: dict) -> dict | None:
        item_id = str(detail.get("id") or list_feature.get("id") or "").strip()
        props = detail.get("properties") if isinstance(detail.get("properties"), dict) else {}
        if not props:
            props = list_feature.get("properties") if isinstance(list_feature.get("properties"), dict) else {}

        title = _clean_space(props.get("title"))
        if not title:
            print(f"[{_SITE_ID}] item {item_id or '?'}: missing title, skipping.")
            return None

        abstract = _build_abstract(props)
        if len(abstract) < _MIN_ABSTRACT_CHARS:
            print(
                f"[{_SITE_ID}] item {item_id}: abstract too short "
                f"({len(abstract)} chars), skipping."
            )
            return None

        created_raw = props.get("created")
        modified_raw = props.get("modified")
        published_date = _ms_to_date(created_raw)
        listed_date = _ms_to_date(modified_raw) or published_date

        original_filename = (
            _clean_space(props.get("name"))
            or _filename_from_url(props.get("url"))
            or None
        )

        data_url = _ARCGIS_DATA_URL.format(item_id=item_id)
        pdf_url = data_url if _is_pdf(props) else None
        if pdf_url and not original_filename:
            original_filename = self._filename_from_head(pdf_url) or _filename_from_url(pdf_url)

        keywords = _join(props.get("tags"), ", ")
        category = _category_string(props.get("categories"))
        publisher = _clean_space(props.get("accessInformation")) or _clean_space(props.get("source")) or None
        owner = _clean_space(props.get("owner")) or None
        department = _clean_space(props.get("source")) or None

        metadata = {
            "posted_date": modified_raw,
            "posted_date_iso": listed_date,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "arcgis_item_id": item_id,
            "node_id": item_id,
            "recordId": detail.get("id"),
            "detail_api_url": self._detail_api_url(item_id),
            "detail_page_url": self._detail_page_url(item_id),
            "download_url": data_url if props.get("name") else None,
            "native_url": props.get("url"),
            "owner": owner,
            "orgId": props.get("orgId"),
            "type": props.get("type"),
            "typeKeywords": props.get("typeKeywords"),
            "created": created_raw,
            "modified": modified_raw,
            "created_iso": published_date,
            "modified_iso": listed_date,
            "categories_raw": props.get("categories"),
            "tags_raw": props.get("tags"),
            "snippet": props.get("snippet"),
            "licenseInfo": props.get("licenseInfo"),
            "license": props.get("license"),
            "access": props.get("access"),
            "source": props.get("source"),
            "size": props.get("size"),
            "numViews": props.get("numViews"),
            "numRatings": props.get("numRatings"),
            "avgRating": props.get("avgRating"),
            "contentStatus": props.get("contentStatus"),
            "culture": props.get("culture"),
            "geometry": detail.get("geometry"),
            "links": detail.get("links"),
            "raw_properties": props,
        }

        return {
            "id": f"{_SITE_ID}:{item_id}",
            "site_id": _SITE_ID,
            "external_id": item_id,
            "post_number": item_id or None,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": owner,
            "publisher": publisher,
            "department": department,
            "journal": None,
            "url": self._detail_page_url(item_id),
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _fetch_json(self, url: str, context: str) -> dict | None:
        raw = self._curl(url, context=context)
        if raw is None:
            return None
        text = raw.decode("utf-8", errors="replace")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            print(f"[{_SITE_ID}] {context}: JSON parse error: {exc}")
            return None
        if isinstance(data, dict) and data.get("statusCode") and data.get("statusCode") >= 400:
            print(f"[{_SITE_ID}] {context}: API error {data.get('statusCode')}: {data.get('message')}")
            return None
        return data if isinstance(data, dict) else None

    def _curl(self, url: str, context: str, head: bool = False) -> bytes | None:
        waits = [1, 3, 9]
        for attempt in range(3):
            if attempt > 0:
                time.sleep(waits[attempt - 1])
            try:
                cmd = ["curl", "--tls-max", "1.3", "-skL", "--max-time", "60"]
                if head:
                    cmd.append("-I")
                cmd.append(url)
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=70,
                )
            except subprocess.TimeoutExpired:
                print(f"[{_SITE_ID}] {context}: curl timed out (attempt {attempt + 1}/3)")
                continue
            except Exception as exc:
                print(f"[{_SITE_ID}] {context}: curl failed (attempt {attempt + 1}/3): {exc}")
                continue

            if result.returncode == 0 and result.stdout:
                return result.stdout

            stderr = result.stderr.decode("utf-8", errors="replace")[:300]
            print(
                f"[{_SITE_ID}] {context}: curl rc={result.returncode} "
                f"(attempt {attempt + 1}/3): {stderr}"
            )

        print(f"[{_SITE_ID}] {context}: network failed after 3 attempts, skipping.")
        return None

    def _filename_from_head(self, url: str) -> str | None:
        raw = self._curl(url, context="filename HEAD", head=True)
        if raw is None:
            return None
        headers = raw.decode("utf-8", errors="replace")
        return _filename_from_content_disposition(headers)

    def _detail_api_url(self, item_id: str) -> str:
        return f"{_API_ITEMS}/{item_id}"

    def _detail_page_url(self, item_id: str) -> str:
        return f"{_BASE_URL}/documents/{item_id}/about"

    def _next_link(self, data: dict) -> str | None:
        links = data.get("links") or []
        for link in links:
            if isinstance(link, dict) and link.get("rel") == "next" and link.get("href"):
                return str(link["href"])
        return None
