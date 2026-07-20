# -*- coding: utf-8 -*-
"""Crawler for data.gov.uk PDF search results.

Public list page:
https://www.data.gov.uk/search?q=&filters%5Bpublisher%5D=&filters%5Btopic%5D=&filters%5Bformat%5D=PDF&sort=best

Discovered CKAN endpoints:
- https://www.data.gov.uk/api/3/action/package_search
- https://www.data.gov.uk/api/3/action/package_show
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import time
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

from crawler.base_crawler import BaseCrawler


_BASE_URL = "https://www.data.gov.uk"
_START_URL = (
    "https://www.data.gov.uk/search?q=&filters%5Bpublisher%5D="
    "&filters%5Btopic%5D=&filters%5Bformat%5D=PDF&sort=best"
)
_PACKAGE_SEARCH_URL = "https://www.data.gov.uk/api/3/action/package_search"
_PACKAGE_SHOW_URL = "https://www.data.gov.uk/api/3/action/package_show"

_ROWS_PER_PAGE = 20
_MAX_PAGES = 200
_WALL_BUDGET_SECONDS = 25 * 60
_ABSTRACT_MIN_CHARS = 50

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)


class DataGovUKSearchCrawler(BaseCrawler):
    site_id = "data-gov-uk-search"
    site_name = "Custom: data-gov-uk-search"
    base_url = _BASE_URL

    def crawl(self, limit=None):
        if limit is not None and limit <= 0:
            return 0

        started_at = time.time()
        saved = 0
        page = 1
        next_url = _START_URL
        seen_urls = set()
        limit_label = limit if limit is not None else "inf"

        while True:
            if limit is not None and saved >= limit:
                break
            if time.time() - started_at > _WALL_BUDGET_SECONDS - 30:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly")
                break
            if page > _MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached; stopping")
                break
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            items, has_next, discovered_next_url = self._fetch_list_page(page, next_url)
            if not items:
                print(f"[{self.site_id}] page {page}: no records; done")
                break

            new_urls_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                url = item.get("url")
                if not url:
                    continue
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_urls_on_page += 1

                try:
                    time.sleep(getattr(self, "_delay", 1.0))
                    did_save = self._process_item(item)
                    if did_save:
                        saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    item_id = item.get("external_id") or item.get("slug") or url
                    print(f"[{self.site_id}] item {item_id} failed: {exc}")
                    continue

            if new_urls_on_page == 0:
                print(f"[{self.site_id}] page {page}: no new URLs; stopping to avoid pagination loop")
                break
            if not has_next:
                print(f"[{self.site_id}] page {page}: next page link absent; done")
                break

            next_url = discovered_next_url or self._search_url_for_page(page + 1)
            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Page and detail flow
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page, page_url):
        raw = self._curl_get(page_url, accept="text/html,application/xhtml+xml,*/*;q=0.8")
        if raw:
            items, has_next, next_url = self._parse_html_list(raw, page_url)
            if items:
                return items, has_next, next_url
            print(f"[{self.site_id}] HTML list page {page} had no parseable rows; trying CKAN API")
        else:
            print(f"[{self.site_id}] HTML list page {page} failed; trying CKAN API")
        return self._fetch_api_list_page(page)

    def _process_item(self, item):
        detail = self._fetch_package(item)
        if not detail:
            print(f"[{self.site_id}] item {item.get('url') or '?'} skipped: detail unavailable")
            return False

        record = self._build_record(item, detail)
        abstract = self._clean_text(record.get("abstract") or "")
        if len(abstract) < _ABSTRACT_MIN_CHARS:
            print(
                f"[{self.site_id}] item {record.get('post_number') or '?'} "
                f"skipped: abstract too short ({len(abstract)} chars)"
            )
            return False

        metadata = dict(record.get("metadata") or {})
        metadata.setdefault("posted_date", record.get("posted_date_raw"))
        metadata.setdefault("originalFilename", record.get("original_filename"))
        metadata.setdefault("journal_raw", record.get("journal_raw"))
        metadata.setdefault("series", record.get("series"))
        metadata.setdefault("volume", record.get("volume"))
        metadata.setdefault("issue", record.get("issue"))
        metadata.setdefault("package_id", record.get("external_id"))
        metadata.setdefault("package_name", record.get("slug"))
        metadata.setdefault("post_number", record.get("post_number"))
        metadata.setdefault("list_endpoint", _START_URL)
        metadata.setdefault("list_api_endpoint", _PACKAGE_SEARCH_URL)
        metadata.setdefault("detail_api_endpoint", _PACKAGE_SHOW_URL)
        metadata.setdefault("format_filter", "PDF")

        paper = {
            "id": None,
            "site_id": self.site_id,
            "external_id": record.get("external_id"),
            "post_number": record.get("post_number"),
            "title": record.get("title"),
            "abstract": abstract,
            "published_date": record.get("published_date"),
            "listed_date": record.get("listed_date"),
            "posted_date": record.get("listed_date"),
            "authors": record.get("authors") or "",
            "publisher": record.get("publisher") or "",
            "department": record.get("department") or "",
            "journal": record.get("journal") or "",
            "url": record.get("url"),
            "pdf_url": record.get("pdf_url"),
            "keywords": record.get("keywords") or "",
            "category": record.get("category") or "",
            "doi": record.get("doi") or "",
            "original_filename": record.get("original_filename"),
            "metadata": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        }
        self._save_paper(paper)
        return True

    def _build_record(self, item, detail):
        extras = self._extras_dict(detail)
        package_id = detail.get("id") or item.get("external_id") or detail.get("name")
        slug = detail.get("name") or item.get("slug")
        post_number = package_id or slug

        title = self._clean_text(detail.get("title") or item.get("title") or slug or "")
        abstract = self._clean_text(detail.get("notes") or item.get("abstract") or "")

        list_posted_raw = item.get("listed_date_raw")
        listed_date = (
            self._parse_date(list_posted_raw)
            or self._parse_date(extras.get("dcat_modified"))
            or self._parse_date(extras.get("metadata-date"))
            or self._parse_date(detail.get("metadata_modified"))
        )
        published_date = (
            self._parse_date(extras.get("dcat_issued"))
            or self._reference_date(extras, "publication")
            or self._reference_date(extras, "creation")
            or self._parse_date(self._first_value(detail.get("temporal_coverage-from")))
            or self._parse_date(detail.get("metadata_created"))
            or listed_date
        )

        org = detail.get("organization") or {}
        publisher = (
            self._clean_text(item.get("publisher"))
            or self._clean_text(org.get("title"))
            or self._clean_text(extras.get("dcat_publisher_name"))
        )
        department = self._clean_text(org.get("name") or "")

        tags = [self._clean_text(t.get("display_name") or t.get("name")) for t in detail.get("tags") or []]
        tags = [t for t in tags if t]
        groups = [self._clean_text(g.get("display_name") or g.get("title") or g.get("name")) for g in detail.get("groups") or []]
        groups = [g for g in groups if g]

        pdf_resource = self._select_pdf_resource(detail.get("resources") or [])
        pdf_url = pdf_resource.get("url") if pdf_resource else None
        original_filename = self._filename_for_resource(pdf_resource, pdf_url) if pdf_resource else None

        doi = self._extract_doi(detail, extras)
        category_parts = groups or [extras.get("resource-type"), detail.get("type"), "PDF dataset"]
        category = "; ".join(self._dedupe(self._clean_text(x) for x in category_parts if x))

        raw_package = dict(detail)
        metadata = {
            "raw_list_item": dict(item),
            "raw_package": raw_package,
            "extras": extras,
            "harvest": detail.get("harvest"),
            "resources": detail.get("resources"),
            "selected_pdf_resource": pdf_resource,
            "organization": org,
            "owner_org": detail.get("owner_org"),
            "creator_user_id": detail.get("creator_user_id"),
            "guid": extras.get("guid"),
            "dcat_issued": extras.get("dcat_issued"),
            "dcat_modified": extras.get("dcat_modified"),
            "metadata_created": detail.get("metadata_created"),
            "metadata_modified": detail.get("metadata_modified"),
            "posted_date": list_posted_raw,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
        }

        return {
            "external_id": package_id,
            "post_number": post_number,
            "slug": slug,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date_raw": list_posted_raw or detail.get("metadata_modified"),
            "authors": "",
            "publisher": publisher,
            "department": department,
            "journal": "",
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "url": item.get("url") or self._dataset_url(package_id, slug),
            "pdf_url": pdf_url,
            "keywords": ", ".join(tags),
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": metadata,
        }

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, accept="*/*", timeout=40):
        return self._curl(url, accept=accept, timeout=timeout, head=False)

    def _curl_head(self, url, accept="*/*", timeout=20):
        return self._curl(url, accept=accept, timeout=timeout, head=True)

    def _curl(self, url, accept="*/*", timeout=40, head=False):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--max-time",
            str(timeout),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            f"Accept: {accept}",
            "-H",
            "Accept-Language: en-GB,en;q=0.9",
        ]
        if head:
            cmd.append("-I")
        cmd.append(url)

        waits = (1, 3, 9)
        last_error = None
        for attempt, wait in enumerate(waits, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 10,
                    check=False,
                )
                stdout = result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace")
                if result.returncode == 0 and stdout.strip():
                    return stdout
                last_error = stderr.strip() or f"curl returned {result.returncode}"
            except Exception as exc:
                last_error = str(exc)

            if attempt < len(waits):
                print(f"[{self.site_id}] network error for {url}: {last_error}; retrying in {wait}s")
                time.sleep(wait)

        print(f"[{self.site_id}] network failed after 3 attempts for {url}: {last_error}")
        return None

    def _fetch_package(self, item):
        package_id = item.get("external_id") or item.get("slug")
        if not package_id:
            return None
        url = f"{_PACKAGE_SHOW_URL}?{urlencode({'id': package_id})}"
        raw = self._curl_get(url, accept="application/json,*/*;q=0.8")
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] detail JSON parse failed for {package_id}: {exc}")
            return None
        if not payload.get("success"):
            print(f"[{self.site_id}] package_show failed for {package_id}: {payload.get('error')}")
            return None
        result = payload.get("result")
        return result if isinstance(result, dict) else None

    def _fetch_api_list_page(self, page):
        start = (page - 1) * _ROWS_PER_PAGE
        params = {
            "q": "",
            "fq": "res_format:PDF",
            "rows": str(_ROWS_PER_PAGE),
            "start": str(start),
            "sort": "score desc",
        }
        raw = self._curl_get(
            f"{_PACKAGE_SEARCH_URL}?{urlencode(params)}",
            accept="application/json,*/*;q=0.8",
        )
        if not raw:
            return [], False, None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] list API JSON parse failed on page {page}: {exc}")
            return [], False, None
        if not payload.get("success"):
            print(f"[{self.site_id}] package_search failed on page {page}: {payload.get('error')}")
            return [], False, None

        result = payload.get("result") or {}
        records = result.get("results") or []
        items = []
        for record in records:
            if not isinstance(record, dict):
                continue
            package_id = record.get("id")
            slug = record.get("name")
            url = self._dataset_url(package_id, slug)
            org = record.get("organization") or {}
            items.append(
                {
                    "title": record.get("title"),
                    "abstract": record.get("notes"),
                    "publisher": org.get("title"),
                    "listed_date_raw": record.get("metadata_modified"),
                    "listed_date": self._parse_date(record.get("metadata_modified")),
                    "url": url,
                    "external_id": package_id or slug,
                    "slug": slug,
                    "source_list_url": f"{_PACKAGE_SEARCH_URL}?{urlencode({'start': start})}",
                    "source": "package_search",
                }
            )
        count = result.get("count") or 0
        has_next = bool(records) and start + len(records) < count
        return items, has_next, self._search_url_for_page(page + 1) if has_next else None

    # ------------------------------------------------------------------
    # HTML list parsing
    # ------------------------------------------------------------------

    def _parse_html_list(self, raw, page_url):
        soup = self._make_soup(raw)
        if soup is None:
            return [], False, None

        rows = soup.select(".dgu-results__result")
        items = []
        for row in rows:
            link = row.select_one("h2 a[href*='/dataset/']") or row.select_one("a[href*='/dataset/']")
            if link is None:
                continue
            href = link.get("href")
            detail_url = urljoin(_BASE_URL, href)
            package_id, slug = self._ids_from_dataset_url(detail_url)

            meta = self._metadata_from_dl(row)
            summary = ""
            paragraph = row.find("p")
            if paragraph is not None:
                summary = self._clean_text(paragraph.get_text(" ", strip=True))

            items.append(
                {
                    "title": self._clean_text(link.get_text(" ", strip=True)),
                    "abstract": summary,
                    "publisher": meta.get("published by"),
                    "listed_date_raw": meta.get("last updated"),
                    "listed_date": self._parse_date(meta.get("last updated")),
                    "url": detail_url,
                    "external_id": package_id or slug,
                    "slug": slug,
                    "source_list_url": page_url,
                    "source": "html_search",
                }
            )

        next_link = soup.select_one("a[rel='next'][href]")
        next_url = urljoin(_BASE_URL, next_link.get("href")) if next_link else None
        return items, bool(next_url), next_url

    def _make_soup(self, raw):
        try:
            from bs4 import BeautifulSoup
        except Exception as exc:
            print(f"[{self.site_id}] BeautifulSoup unavailable: {exc}")
            return None

        last_error = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                last_error = exc
                continue
        print(f"[{self.site_id}] HTML parse failed with all parsers: {last_error}")
        return None

    def _metadata_from_dl(self, root):
        out = {}
        for dt in root.select("dt"):
            key = self._clean_text(dt.get_text(" ", strip=True)).rstrip(":").lower()
            dd = dt.find_next_sibling("dd")
            if not key or dd is None:
                continue
            out[key] = self._clean_text(dd.get_text(" ", strip=True))
        return out

    # ------------------------------------------------------------------
    # Record helpers
    # ------------------------------------------------------------------

    def _select_pdf_resource(self, resources):
        for resource in resources:
            if not isinstance(resource, dict):
                continue
            if self._resource_is_pdf(resource):
                return resource
        return None

    def _resource_is_pdf(self, resource):
        values = [
            resource.get("format"),
            resource.get("mimetype"),
            resource.get("mimetype_inner"),
            resource.get("name"),
            resource.get("url"),
        ]
        text = " ".join(str(v or "") for v in values).lower()
        return "pdf" in text or "application/pdf" in text

    def _filename_for_resource(self, resource, pdf_url):
        candidates = []
        if pdf_url:
            parsed = urlparse(pdf_url)
            query = parse_qs(parsed.query)
            for key in ("filename", "fileName", "file", "name"):
                for value in query.get(key, []):
                    candidates.append(value)
            tail = unquote(parsed.path.rstrip("/").split("/")[-1])
            candidates.append(tail)
        if resource:
            candidates.extend([resource.get("name"), resource.get("description")])

        for candidate in candidates:
            filename = self._normalize_filename(candidate)
            if filename:
                return filename

        disposition = self._content_disposition_filename(pdf_url)
        if disposition:
            return disposition
        return None

    def _content_disposition_filename(self, url):
        if not url:
            return None
        raw = self._curl_head(url, accept="application/pdf,*/*;q=0.8", timeout=20)
        if not raw:
            return None
        header_value = None
        for line in raw.splitlines():
            if line.lower().startswith("content-disposition:"):
                header_value = line.split(":", 1)[1].strip()
        if not header_value:
            return None
        match = re.search(r"filename\*=UTF-8''([^;\r\n]+)", header_value, re.IGNORECASE)
        if not match:
            match = re.search(r'filename="?([^";\r\n]+)"?', header_value, re.IGNORECASE)
        if not match:
            return None
        return self._normalize_filename(unquote(match.group(1).strip()))

    def _normalize_filename(self, value):
        if not value:
            return None
        text = self._clean_text(str(value)).strip().strip("'\"")
        if not text or text.lower() in {"download", "pdf", "application/pdf"}:
            return None
        text = text.split("/")[-1]
        if "." not in text:
            return None
        if len(text) > 240:
            return None
        return text

    def _extras_dict(self, detail):
        extras = {}
        for item in detail.get("extras") or []:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            if key:
                extras[key] = item.get("value")
        for item in detail.get("harvest") or []:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            if key and key not in extras:
                extras[key] = item.get("value")
        return extras

    def _reference_date(self, extras, date_type):
        raw = extras.get("dataset-reference-date")
        if not raw:
            return None
        try:
            values = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(values, list):
            return None
        for item in values:
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "").lower() == date_type:
                parsed = self._parse_date(item.get("value"))
                if parsed:
                    return parsed
        return None

    def _extract_doi(self, detail, extras):
        values = [
            detail.get("url"),
            detail.get("notes"),
            detail.get("title"),
            extras.get("guid"),
            extras.get("doi"),
            extras.get("identifier"),
        ]
        for value in values:
            if not value:
                continue
            match = _DOI_RE.search(str(value))
            if match:
                return match.group(0).rstrip(".,);")
        return None

    def _parse_date(self, value):
        if not value:
            return None
        if isinstance(value, list):
            value = self._first_value(value)
        text = self._clean_text(str(value))
        match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
        if match:
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"

        text = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", text, flags=re.IGNORECASE)
        for fmt in (
            "%d %B %Y",
            "%d %b %Y",
            "%B %d %Y",
            "%b %d %Y",
            "%a, %d %b %Y %H:%M:%S %z",
            "%a, %d %b %Y %H:%M:%S %Z",
        ):
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                continue
        return None

    def _ids_from_dataset_url(self, url):
        parsed = urlparse(url)
        parts = [unquote(p) for p in parsed.path.split("/") if p]
        if not parts or parts[0] != "dataset":
            return None, None
        if len(parts) >= 2 and _UUID_RE.match(parts[1]):
            return parts[1], parts[2] if len(parts) >= 3 else None
        return None, parts[1] if len(parts) >= 2 else None

    def _dataset_url(self, package_id, slug):
        if package_id and slug:
            return f"{_BASE_URL}/dataset/{package_id}/{slug}"
        if slug:
            return f"{_BASE_URL}/dataset/{slug}"
        if package_id:
            return f"{_BASE_URL}/dataset/{package_id}"
        return _BASE_URL

    def _search_url_for_page(self, page):
        if page <= 1:
            return _START_URL
        return (
            f"{_BASE_URL}/search.html?"
            f"{urlencode({'filters[format]': 'PDF', 'filters[publisher]': '', 'filters[topic]': '', 'page': page, 'q': '', 'sort': 'best'})}"
        )

    def _clean_text(self, value):
        if value is None:
            return ""
        text = html.unescape(str(value))
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def _first_value(self, value):
        if isinstance(value, list):
            for item in value:
                if item not in (None, ""):
                    return item
            return None
        return value

    def _dedupe(self, values):
        seen = set()
        out = []
        for value in values:
            if not value:
                continue
            if value in seen:
                continue
            seen.add(value)
            out.append(value)
        return out
