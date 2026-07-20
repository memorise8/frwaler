# -*- coding: utf-8 -*-
"""WODC documents crawler for annual-report related records.

Discovery notes:
  The requested WODC page (https://www.wodc.nl/documenten?type=Jaarverslag)
  is now a Next.js shell that points publication users to the WODC DSpace
  repository. The real list API is:

    https://repository.wodc.nl/server/api/discover/search/objects?query=Jaarverslag

  Search results embed item metadata. Detail/PDF data is available through:

    https://repository.wodc.nl/server/api/core/items/{uuid}/bundles?embed=bitstreams
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote, unquote, urljoin

# Absolute import -- spec_from_file_location has no package context.
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from crawler.base_crawler import BaseCrawler  # noqa: E402


def _clean(text):
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def _make_soup(raw):
    """Parse HTML with fallback chain: html5lib -> lxml -> html.parser."""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup

            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _html_to_text(raw_html):
    if not raw_html:
        return ""
    soup = _make_soup(raw_html)
    if soup:
        return _clean(soup.get_text(" ", strip=True))
    return _clean(re.sub(r"<[^>]+>", " ", str(raw_html)))


def _curl(url, *, accept="application/json", method="GET", timeout=45, retries=3):
    """Fetch URL via curl with TLS max 1.3 and exponential backoff."""
    waits = [1, 3, 9]
    cmd = [
        "curl",
        "-skL",
        "--tls-max",
        "1.3",
        "--max-time",
        str(timeout),
        "-X",
        method,
        "-H",
        (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "-H",
        f"Accept: {accept}",
        url,
    ]
    for attempt in range(retries):
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=timeout + 10, check=False)
            body = (res.stdout or b"").decode("utf-8", errors="replace")
            if res.returncode == 0 and body.strip():
                return body
            err = (res.stderr or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(err or f"curl exit {res.returncode}; empty response")
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            if attempt < retries - 1:
                wait = waits[attempt]
                print(f"[wodc-nl-documenten] curl failed ({attempt + 1}/{retries}) {url}: {exc}; retry in {wait}s")
                time.sleep(wait)
            else:
                print(f"[wodc-nl-documenten] curl failed after {retries}: {url}: {exc}")
    return None


def _curl_json(url, *, timeout=45, retries=3):
    raw = _curl(url, accept="application/json, application/hal+json, */*", timeout=timeout, retries=retries)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception as exc:
        print(f"[wodc-nl-documenten] JSON parse failed for {url}: {exc}")
        return None
    if isinstance(data, dict) and int(data.get("status") or 0) >= 400:
        print(f"[wodc-nl-documenten] API error for {url}: {data.get('status')} {data.get('message') or data.get('error')}")
        return None
    return data


def _metadata_values(metadata, key):
    values = []
    for entry in (metadata or {}).get(key, []) or []:
        value = entry.get("value") if isinstance(entry, dict) else entry
        value = _clean(value)
        if value:
            values.append(value)
    return values


def _metadata_first(metadata, *keys):
    for key in keys:
        values = _metadata_values(metadata, key)
        if values:
            return values[0]
    return ""


def _join_unique(values, sep):
    seen = set()
    out = []
    for value in values:
        value = _clean(value)
        marker = value.lower()
        if value and marker not in seen:
            out.append(value)
            seen.add(marker)
    return sep.join(out) if out else None


def _iso_date(raw):
    raw = _clean(raw)
    if not raw:
        return None
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if match:
        return match.group(0)
    match = re.fullmatch(r"(\d{4})-(\d{2})", raw)
    if match:
        return f"{match.group(1)}-{match.group(2)}-01"
    match = re.fullmatch(r"(\d{4})", raw)
    if match:
        return f"{match.group(1)}-01-01"
    return raw[:10] if raw else None


def _post_number(item, metadata):
    project = _metadata_first(metadata, "dc.identifier.project")
    if project:
        match = re.search(r"\d+", project)
        return match.group(0) if match else project
    handle = _clean(item.get("handle"))
    if handle:
        tail = handle.rstrip("/").split("/")[-1]
        return tail or handle
    uuid_val = _clean(item.get("uuid") or item.get("id"))
    return uuid_val or None


def _display_item_url(item):
    uuid_val = _clean(item.get("uuid") or item.get("id"))
    if uuid_val:
        return f"https://repository.wodc.nl/items/{uuid_val}"
    handle_uri = _metadata_first(item.get("metadata") or {}, "dc.identifier.uri")
    return handle_uri or ""


def _filename_from_url(url):
    if not url:
        return None
    tail = unquote(url.rstrip("/").split("/")[-1].split("?")[0].split("#")[0])
    return tail if "." in tail and len(tail) <= 240 else None


def _is_annual_report_record(item):
    metadata = item.get("metadata") or {}
    candidates = []
    candidates.extend(_metadata_values(metadata, "dc.title"))
    candidates.extend(_metadata_values(metadata, "dc.title.english"))
    candidates.extend(_metadata_values(metadata, "dc.subject"))
    candidates.extend(_metadata_values(metadata, "dc.type"))
    candidates.append(item.get("name") or "")
    haystack = " | ".join(candidates).lower()
    return "jaarverslag" in haystack or "annual report" in haystack


def _extract_objects(data):
    result = (((data or {}).get("_embedded") or {}).get("searchResult") or {})
    objects = ((result.get("_embedded") or {}).get("objects") or [])
    items = []
    for wrapper in objects:
        item = ((wrapper.get("_embedded") or {}).get("indexableObject") or {})
        if item:
            items.append(item)
    return items


def _has_next_page(data):
    result = (((data or {}).get("_embedded") or {}).get("searchResult") or {})
    links = result.get("_links") or {}
    if links.get("next", {}).get("href"):
        return True
    page = result.get("page") or {}
    try:
        return int(page.get("number", 0)) + 1 < int(page.get("totalPages", 0))
    except Exception:
        return False


class WodcNlDocumentenCrawler(BaseCrawler):
    site_id = "wodc-nl-documenten"
    site_name = "Custom: wodc-nl-documenten"
    base_url = "https://www.wodc.nl"

    _START_URL = "https://www.wodc.nl/documenten?type=Jaarverslag"
    _REPOSITORY_BASE = "https://repository.wodc.nl"
    _SEARCH_API = "https://repository.wodc.nl/server/api/discover/search/objects"
    _PAGE_SIZE = 100
    _MAX_PAGES = 200
    _MAX_SECONDS = 25 * 60

    def _repository_base_from_start_page(self):
        html = _curl(self._START_URL, accept="text/html,application/xhtml+xml,*/*;q=0.8", timeout=30, retries=3)
        if not html:
            return self._REPOSITORY_BASE
        soup = _make_soup(html)
        if not soup:
            return self._REPOSITORY_BASE
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"].strip()
            if "repository.wodc.nl" in href:
                return href.rstrip("/")
        return self._REPOSITORY_BASE

    def _list_url(self, api_page):
        query = quote("Jaarverslag")
        return (
            f"{self._SEARCH_API}?query={query}"
            f"&size={self._PAGE_SIZE}&page={api_page}"
            "&sort=dc.date.issued,DESC"
        )

    def _detail_bundles_url(self, item):
        uuid_val = _clean(item.get("uuid") or item.get("id"))
        if not uuid_val:
            return None
        return f"{self._REPOSITORY_BASE}/server/api/core/items/{uuid_val}/bundles?embed=bitstreams"

    def _extract_pdf(self, bundles_data):
        bundles = (((bundles_data or {}).get("_embedded") or {}).get("bundles") or [])
        candidates = []
        for bundle in bundles:
            bundle_name = _clean(bundle.get("name")).upper()
            bitstreams = (
                (((bundle.get("_embedded") or {}).get("bitstreams") or {}).get("_embedded") or {}).get("bitstreams")
                or []
            )
            for bitstream in bitstreams:
                name = _clean(bitstream.get("name"))
                content_url = ((bitstream.get("_links") or {}).get("content") or {}).get("href")
                uuid_val = _clean(bitstream.get("uuid") or bitstream.get("id"))
                if not name and content_url:
                    name = _filename_from_url(content_url) or ""
                if not content_url and uuid_val:
                    content_url = f"{self._REPOSITORY_BASE}/server/api/core/bitstreams/{uuid_val}/content"
                is_pdf = name.lower().endswith(".pdf") or "pdf" in _clean(bitstream.get("bundleName")).lower()
                if bundle_name == "ORIGINAL" and content_url and is_pdf:
                    download_url = f"{self._REPOSITORY_BASE}/bitstreams/{uuid_val}/download" if uuid_val else content_url
                    candidates.append((download_url, name or _filename_from_url(download_url), bitstream))
        if not candidates:
            return None, None, None
        return candidates[0]

    def _paper_from_item(self, item, bundles_data, list_page):
        metadata = item.get("metadata") or {}
        title = _metadata_first(metadata, "dc.title") or _clean(item.get("name"))

        abstract = _metadata_first(metadata, "dc.description.abstract")
        if len(abstract) < 50:
            abstract = _html_to_text(_metadata_first(metadata, "html.description.abstract"))

        published_raw = _metadata_first(metadata, "dc.date.issued")
        listed_raw = _metadata_first(metadata, "dc.date.accessioned", "dc.date.available", "refterms.dateFOA")
        published_date = _iso_date(published_raw)
        listed_date = _iso_date(listed_raw) or published_date

        authors = _join_unique(_metadata_values(metadata, "dc.contributor.author"), "; ")
        publishers = _join_unique(
            _metadata_values(metadata, "dc.publisher") + _metadata_values(metadata, "dc.contributor.institution"),
            "; ",
        )
        keywords = _join_unique(_metadata_values(metadata, "dc.subject"), ", ")
        category = _join_unique(_metadata_values(metadata, "dc.type"), ", ")
        series = _join_unique(_metadata_values(metadata, "dc.relation.ispartofseries"), "; ")
        external_id = _clean(item.get("uuid") or item.get("id") or item.get("handle"))
        post_number = _post_number(item, metadata)
        url = _display_item_url(item)

        pdf_url, original_filename, pdf_bitstream = self._extract_pdf(bundles_data)
        original_filename = original_filename or _filename_from_url(pdf_url)

        raw_metadata = {
            "posted_date": listed_raw,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": series,
            "volume": None,
            "issue": None,
            "node_id": external_id,
            "uuid": item.get("uuid") or item.get("id"),
            "handle": item.get("handle"),
            "post_number": post_number,
            "published_date_raw": published_raw,
            "listed_date_raw": listed_raw,
            "list_page": list_page,
            "source_start_url": self._START_URL,
            "list_api": self._SEARCH_API,
            "detail_api": self._detail_bundles_url(item),
            "metadata": metadata,
            "pdf_bitstream": pdf_bitstream,
        }

        return {
            "id": external_id,
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": authors,
            "publisher": publishers,
            "department": None,
            "journal": None,
            "url": url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": _metadata_first(metadata, "dc.identifier.doi") or None,
            "original_filename": original_filename,
            "metadata": json.dumps(raw_metadata, ensure_ascii=False),
        }

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        start_time = time.time()
        limit_or_inf = str(limit) if limit is not None else "∞"

        repository_base = self._repository_base_from_start_page()
        if repository_base != self._REPOSITORY_BASE:
            self._REPOSITORY_BASE = repository_base

        for api_page in range(self._MAX_PAGES):
            page_no = api_page + 1

            if time.time() - start_time > self._MAX_SECONDS:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; stopping cleanly.")
                break
            if limit is not None and saved >= limit:
                break
            if page_no % 10 == 0:
                print(f"[{self.site_id}] page {page_no}: saved {saved}/{limit_or_inf}")

            data = _curl_json(self._list_url(api_page), timeout=60, retries=3)
            if not data:
                print(f"[{self.site_id}] page {page_no}: list API failed; stopping.")
                break

            items = _extract_objects(data)
            if not items:
                print(f"[{self.site_id}] page {page_no}: no records; done.")
                break

            new_items = []
            for item in items:
                item_url = _display_item_url(item)
                if not item_url:
                    continue
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                new_items.append(item)

            if not new_items:
                print(f"[{self.site_id}] page {page_no}: all records already seen; stopping.")
                break

            for item in new_items:
                if limit is not None and saved >= limit:
                    break
                item_url = _display_item_url(item)
                try:
                    if not _is_annual_report_record(item):
                        continue

                    time.sleep(self._delay)
                    bundles_url = self._detail_bundles_url(item)
                    bundles_data = _curl_json(bundles_url, timeout=45, retries=3) if bundles_url else None
                    paper = self._paper_from_item(item, bundles_data, page_no)

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < 50:
                        print(f"[{self.site_id}] skipping (abstract <50 chars): {item_url}")
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:70]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_url or '?'} failed: {exc}")
                    continue

            if not _has_next_page(data):
                print(f"[{self.site_id}] page {page_no}: next page absent; done.")
                break
        else:
            print(f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached.")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
