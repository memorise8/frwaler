# -*- coding: utf-8 -*-
"""Crawler for search.open.canada.ca open-data PDF publications.

The public search page is backed by CKAN.  The list endpoint below matches the
requested search page count and filters; each item is then refreshed through
package_show so we parse the canonical detail record.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from html import unescape
from urllib.parse import unquote, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_THIS_DIR)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402


class SearchOpenCanadaCaOpendataCrawler(BaseCrawler):
    site_id = "search-open-canada-ca-opendata"
    site_name = "Custom: search-open-canada-ca-opendata"
    base_url = "https://search.open.canada.ca"

    START_URL = (
        "https://search.open.canada.ca/opendata/"
        "?resource_type=report%7Cpublication%7Cannual_report"
        "&page=1&sort=metadata_modified+desc&resource_format=PDF"
    )
    LIST_API_URL = "https://open.canada.ca/data/api/3/action/package_search"
    DETAIL_API_URL = "https://open.canada.ca/data/api/3/action/package_show"
    DETAIL_PAGE_URL = "https://open.canada.ca/data/en/dataset/{package_id}"

    RESOURCE_TYPES = ("report", "publication", "annual_report")
    LIST_FQ = (
        "res_format:PDF AND "
        "(res_type:report OR res_type:publication OR res_type:annual_report)"
    )
    SORT = "metadata_modified desc"
    PAGE_SIZE = 10
    MAX_PAGES = 200
    WALL_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    BACKOFF_SECONDS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 50

    def crawl(self, limit=None):
        saved = 0
        page = 1
        start_ts = time.time()
        limit_or_inf = limit if limit is not None else "inf"
        seen_urls = set()

        while True:
            if limit is not None and saved >= limit:
                break

            if page > self.MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {self.MAX_PAGES} pages reached")
                break

            if self._approaching_wall_budget(start_ts):
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            page_records, has_next = self._fetch_list_page(page)
            if not page_records:
                print(f"[{self.site_id}] page {page}: no records returned; stopping")
                break

            new_urls_on_page = 0
            for offset, list_record in enumerate(page_records, start=1):
                if limit is not None and saved >= limit:
                    break

                if self._approaching_wall_budget(start_ts):
                    print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly")
                    return saved

                package_id = self._record_id(list_record)
                item_label = package_id or f"page {page} item {offset}"

                try:
                    if not package_id:
                        raise RuntimeError("missing package id")

                    detail_url = self.DETAIL_PAGE_URL.format(package_id=package_id)
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_urls_on_page += 1

                    time.sleep(self._delay)
                    package = self._fetch_detail_package(package_id)
                    if package is None:
                        package = self._fetch_detail_html_fallback(package_id)
                    if package is None:
                        raise RuntimeError("detail fetch failed after retries")

                    paper = self._build_paper(package, list_record)
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if new_urls_on_page == 0:
                print(f"[{self.site_id}] page {page}: no new URLs; stopping")
                break

            if not has_next:
                print(f"[{self.site_id}] page {page}: next page absent; stopping")
                break

            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    def _fetch_list_page(self, page):
        data = self._fetch_list_api_page(page)
        if data:
            return data

        print(f"[{self.site_id}] list API failed at page {page}; trying HTML search page")
        return self._fetch_list_html_page(page)

    def _fetch_list_api_page(self, page):
        start = (page - 1) * self.PAGE_SIZE
        params = {
            "fq": self.LIST_FQ,
            "sort": self.SORT,
            "rows": str(self.PAGE_SIZE),
            "start": str(start),
        }
        url = f"{self.LIST_API_URL}?{urlencode(params)}"
        data = self._curl_json(url, context=f"list page {page}")
        if not data or not data.get("success"):
            return None

        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        records = result.get("results") if isinstance(result.get("results"), list) else []
        total = result.get("count")
        has_next = len(records) == self.PAGE_SIZE
        if isinstance(total, int):
            has_next = start + len(records) < total
        return records, has_next

    def _fetch_list_html_page(self, page):
        raw = self._curl_get(
            self._search_page_url(page),
            context=f"HTML list page {page}",
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        )
        if not raw:
            return [], False

        soup = self._make_soup(raw)
        if soup is None:
            return [], False

        records = []
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            package_id = self._package_id_from_url(href)
            if not package_id:
                continue

            title = self._clean_text(link)
            container = link
            for _ in range(5):
                parent = container.find_parent("div")
                if parent is None:
                    break
                container = parent
                if "Record Modified:" in container.get_text(" ", strip=True):
                    break

            text = container.get_text(" ", strip=True) if container else ""
            records.append(
                {
                    "id": package_id,
                    "name": package_id,
                    "title": title,
                    "title_translated": {"en": title},
                    "metadata_modified": self._extract_labeled_text(text, "Record Modified"),
                    "portal_release_date": self._extract_labeled_text(text, "Record Released"),
                    "org_title_at_publication": {
                        "en": self._extract_labeled_text(text, "Publisher")
                    },
                    "_source": "html_list",
                    "_detail_url": self.DETAIL_PAGE_URL.format(package_id=package_id),
                }
            )

        seen = set()
        unique_records = []
        for record in records:
            record_id = self._record_id(record)
            if record_id in seen:
                continue
            seen.add(record_id)
            unique_records.append(record)

        has_next = soup.find("li", class_="next") is not None
        return unique_records, has_next

    def _fetch_detail_package(self, package_id):
        url = f"{self.DETAIL_API_URL}?{urlencode({'id': package_id})}"
        data = self._curl_json(url, context=f"item {package_id} package_show")
        if not data or not data.get("success"):
            return None
        package = data.get("result")
        return package if isinstance(package, dict) else None

    def _fetch_detail_html_fallback(self, package_id):
        raw = self._curl_get(
            self.DETAIL_PAGE_URL.format(package_id=package_id),
            context=f"item {package_id} HTML detail",
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        )
        if not raw:
            return None
        soup = self._make_soup(raw)
        if soup is None:
            return None
        return self._package_from_detail_html(soup, package_id)

    def _build_paper(self, package, list_record):
        package_id = self._record_id(package)
        if not package_id:
            raise RuntimeError("detail package has no native id")

        title = self._localized(package.get("title_translated")) or package.get("title")
        title = self._clean_text(title)
        if not title:
            raise RuntimeError("detail package has no title")

        abstract = self._localized(package.get("notes_translated")) or package.get("notes")
        abstract = self._clean_text(abstract)

        resources = package.get("resources") if isinstance(package.get("resources"), list) else []
        pdf_resource = self._select_pdf_resource(resources)
        pdf_url = self._absolute_url(pdf_resource.get("url")) if pdf_resource else None
        original_filename = self._filename_from_url(pdf_url)

        published_raw = (
            package.get("date_published")
            or (pdf_resource or {}).get("date_published")
            or package.get("portal_release_date")
            or package.get("metadata_created")
        )
        listed_raw = (
            list_record.get("portal_release_date")
            or package.get("portal_release_date")
            or list_record.get("metadata_created")
            or package.get("metadata_created")
        )
        modified_raw = list_record.get("metadata_modified") or package.get("metadata_modified")

        publisher = (
            self._localized(package.get("org_title_at_publication"))
            or self._organization_title(package.get("organization"))
        )
        publisher = self._clean_publisher(publisher)
        department = self._localized(package.get("org_section"))
        department = self._clean_text(department)

        authors = self._author_string(package)
        keywords = self._dedupe(
            self._localized_list(package.get("keywords"))
            + self._string_list(package.get("subject"))
            + self._string_list(package.get("topic_category"))
        )
        category = self._category(package, pdf_resource)
        journal = self._localized(package.get("journal")) or ""
        doi = self._clean_text(package.get("digital_object_identifier") or "")

        metadata = self._metadata_dict(
            package=package,
            list_record=list_record,
            pdf_resource=pdf_resource,
            listed_raw=listed_raw,
            modified_raw=modified_raw,
            original_filename=original_filename,
            publisher=publisher,
            department=department,
        )

        return {
            "id": package_id,
            "site_id": self.site_id,
            "external_id": package_id,
            "post_number": package_id,
            "title": title,
            "abstract": abstract,
            "published_date": self._normalize_date(published_raw),
            "listed_date": self._normalize_date(listed_raw),
            "posted_date": self._normalize_date(listed_raw),
            "authors": authors,
            "publisher": publisher or None,
            "department": department or None,
            "journal": journal or None,
            "url": self.DETAIL_PAGE_URL.format(package_id=package_id),
            "pdf_url": pdf_url,
            "keywords": ", ".join(keywords) if keywords else None,
            "category": category or None,
            "doi": doi or None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        }

    def _metadata_dict(
        self,
        package,
        list_record,
        pdf_resource,
        listed_raw,
        modified_raw,
        original_filename,
        publisher,
        department,
    ):
        data_series = package.get("data_series_name")
        series = self._localized(data_series) or self._localized(package.get("series_publication_dates"))
        issue = self._localized(package.get("data_series_issue_identification"))

        metadata = {
            "start_url": self.START_URL,
            "list_api": self.LIST_API_URL,
            "detail_api": self.DETAIL_API_URL,
            "source": "ckan_package_show",
            "posted_date": listed_raw,
            "record_modified": modified_raw,
            "originalFilename": original_filename,
            "journal_raw": self._localized(package.get("journal")),
            "series": series,
            "volume": self._clean_text(package.get("volume") or ""),
            "issue": issue,
            "package_id": self._record_id(package),
            "name": package.get("name"),
            "owner_org": package.get("owner_org"),
            "organization_id": self._organization_id(package.get("organization")),
            "resource_id": pdf_resource.get("id") if pdf_resource else None,
            "resource_unique_identifier": (
                pdf_resource.get("unique_identifier") if pdf_resource else None
            ),
            "resource_type": pdf_resource.get("resource_type") if pdf_resource else None,
            "resource_format": pdf_resource.get("format") if pdf_resource else None,
            "resource_language": pdf_resource.get("language") if pdf_resource else None,
            "resource_metadata_modified": (
                pdf_resource.get("metadata_modified") if pdf_resource else None
            ),
            "metadata_created": package.get("metadata_created"),
            "metadata_modified": package.get("metadata_modified"),
            "portal_release_date": package.get("portal_release_date"),
            "date_published": package.get("date_published"),
            "publisher": publisher,
            "department": department,
            "raw_list_record": list_record,
            "raw_package": package,
        }
        return {key: value for key, value in metadata.items() if value not in ("", None)}

    def _package_from_detail_html(self, soup, package_id):
        title = self._clean_text(soup.find("h1"))
        description = ""
        desc_tag = soup.find(attrs={"property": "description"})
        if desc_tag:
            description = desc_tag.get("content") or desc_tag.get("value") or desc_tag.get_text(" ")
        if not description:
            meta = soup.find("meta", attrs={"name": "description"})
            if meta:
                description = meta.get("content") or ""

        resources = []
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            if ".pdf" not in href.lower():
                continue
            resources.append(
                {
                    "id": link.get("data-res-id") or "",
                    "format": "PDF",
                    "url": urljoin("https://open.canada.ca", href),
                    "name": self._clean_text(link),
                    "language": ["en"],
                    "resource_type": "publication",
                }
            )

        return {
            "id": package_id,
            "name": package_id,
            "title": title,
            "title_translated": {"en": title},
            "notes": self._clean_text(description),
            "notes_translated": {"en": self._clean_text(description)},
            "resources": resources,
            "metadata_modified": self._html_property_value(soup, "dateModified"),
            "portal_release_date": self._about_record_value(soup, "Record Released"),
            "org_title_at_publication": {
                "en": self._about_record_value(soup, "Publisher - Current Organization Name")
            },
        }

    def _curl_json(self, url, context):
        raw = self._curl_get(
            url,
            context=context,
            accept="application/json,text/javascript,*/*;q=0.8",
        )
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError as exc:
            print(f"[{self.site_id}] {context} invalid JSON: {exc}")
            return None
        return data if isinstance(data, dict) else None

    def _curl_get(self, url, context, accept):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--fail",
            "--max-time",
            "45",
            "--connect-timeout",
            "15",
            "-A",
            self.USER_AGENT,
            "-H",
            f"Accept: {accept}",
            "-H",
            "Accept-Language: en-CA,en;q=0.9",
            url,
        ]

        last_error = ""
        for attempt, wait in enumerate(self.BACKOFF_SECONDS, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=55)
                body = result.stdout or b""
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                if result.returncode == 0 and body.strip():
                    return body.decode("utf-8", errors="replace")
                last_error = stderr or f"curl exit {result.returncode}; empty response"
            except subprocess.TimeoutExpired as exc:
                last_error = f"curl timeout after {exc.timeout}s"
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                last_error = str(exc)

            print(
                f"[{self.site_id}] {context} curl failed "
                f"(attempt {attempt}/3): {last_error}"
            )
            if attempt < 3:
                print(f"[{self.site_id}] retrying in {wait}s...")
                time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None

    def _make_soup(self, raw):
        last_error = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                last_error = exc
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed: {exc}")
        print(f"[{self.site_id}] all BeautifulSoup parsers failed: {last_error}")
        return None

    def _search_page_url(self, page):
        params = {
            "resource_type": "report|publication|annual_report",
            "page": str(page),
            "sort": "metadata_modified desc",
            "resource_format": "PDF",
        }
        return f"{self.base_url}/opendata/?{urlencode(params)}"

    def _select_pdf_resource(self, resources):
        pdf_resources = [resource for resource in resources if self._is_pdf_resource(resource)]
        if not pdf_resources:
            return None

        def rank(resource):
            languages = {lang.lower() for lang in self._string_list(resource.get("language"))}
            res_type = self._clean_text(resource.get("resource_type")).lower()
            is_english = "en" in languages or "eng" in languages
            in_scope_type = res_type in set(self.RESOURCE_TYPES)
            ends_pdf = str(resource.get("url") or "").lower().split("?", 1)[0].endswith(".pdf")
            date_key = (
                self._normalize_datetime(resource.get("metadata_modified"))
                or self._normalize_datetime(resource.get("last_modified"))
                or self._normalize_datetime(resource.get("date_published"))
                or ""
            )
            position = resource.get("position")
            try:
                position_score = -int(position)
            except (TypeError, ValueError):
                position_score = 0
            return (in_scope_type, is_english, ends_pdf, date_key, position_score)

        return sorted(pdf_resources, key=rank, reverse=True)[0]

    def _is_pdf_resource(self, resource):
        fmt = self._clean_text(resource.get("format")).upper()
        url = str(resource.get("url") or "").lower()
        mimetype = str(resource.get("mimetype") or "").lower()
        return fmt == "PDF" or url.split("?", 1)[0].endswith(".pdf") or "pdf" in mimetype

    def _author_string(self, package):
        authors = []
        for key in ("creator", "author", "contributor"):
            value = package.get(key)
            if isinstance(value, dict):
                value = self._localized(value)
            authors.extend(self._string_list(value))
        authors = self._dedupe(authors)
        return "; ".join(authors) if authors else None

    def _category(self, package, pdf_resource):
        parts = []
        if package.get("collection"):
            parts.append(self._clean_text(package.get("collection")))
        if package.get("type"):
            parts.append(self._clean_text(package.get("type")))
        if pdf_resource and pdf_resource.get("resource_type"):
            parts.append(self._clean_text(pdf_resource.get("resource_type")))
        return "; ".join(self._dedupe([part for part in parts if part]))

    def _record_id(self, record):
        if not isinstance(record, dict):
            return ""
        return self._clean_text(record.get("id") or record.get("name") or "")

    def _package_id_from_url(self, href):
        match = re.search(
            r"/data/(?:en|fr)/dataset/"
            r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
            href,
        )
        return match.group(1) if match else ""

    def _absolute_url(self, url):
        if not url:
            return None
        return urljoin("https://open.canada.ca", str(url).strip())

    @staticmethod
    def _filename_from_url(url):
        if not url:
            return None
        path = urlparse(url).path
        tail = unquote(path.rstrip("/").split("/")[-1])
        if "." in tail and len(tail) <= 240:
            return tail
        return None

    def _localized(self, value):
        if isinstance(value, dict):
            for key in ("en", "eng", "en-CA", "en_CA"):
                candidate = value.get(key)
                if candidate:
                    return candidate
            for candidate in value.values():
                if candidate:
                    return candidate
            return ""
        return "" if value is None else str(value)

    def _localized_list(self, value):
        if isinstance(value, dict):
            for key in ("en", "eng", "en-CA", "en_CA"):
                items = value.get(key)
                if items:
                    return self._string_list(items)
            for items in value.values():
                if items:
                    return self._string_list(items)
            return []
        return self._string_list(value)

    def _string_list(self, value):
        if value is None or value == "":
            return []
        if isinstance(value, (list, tuple, set)):
            return [self._clean_text(item) for item in value if self._clean_text(item)]
        if isinstance(value, dict):
            return [self._clean_text(v) for v in value.values() if self._clean_text(v)]
        return [self._clean_text(value)] if self._clean_text(value) else []

    @staticmethod
    def _dedupe(items):
        out = []
        seen = set()
        for item in items:
            if not item:
                continue
            key = item.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    @staticmethod
    def _clean_text(value):
        if value is None:
            return ""
        if hasattr(value, "get_text"):
            value = value.get_text(" ", strip=True)
        text = unescape(str(value))
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"[*_`#>]+", " ", text)
        text = re.sub(r"\s+", " ", text.replace("\r", " ").replace("\n", " "))
        return text.strip()

    def _clean_publisher(self, value):
        text = self._clean_text(value)
        if " | " in text:
            return text.split(" | ", 1)[0].strip()
        return text

    def _organization_title(self, value):
        if not isinstance(value, dict):
            return ""
        return self._clean_text(value.get("title") or value.get("name") or "")

    @staticmethod
    def _organization_id(value):
        if isinstance(value, dict):
            return value.get("id")
        return None

    def _normalize_date(self, value):
        dt = self._normalize_datetime(value)
        return dt[:10] if dt else None

    def _normalize_datetime(self, value):
        text = self._clean_text(value)
        if not text:
            return ""
        match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
        if match:
            year, month, day = match.groups()
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        for fmt in ("%b %d, %Y", "%B %d, %Y"):
            try:
                return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return ""

    def _extract_labeled_text(self, text, label):
        pattern = rf"{re.escape(label)}:\s*(.*?)(?=\s+[A-Z][A-Za-z -]+:|$)"
        match = re.search(pattern, text)
        return self._clean_text(match.group(1)) if match else ""

    def _html_property_value(self, soup, prop):
        tag = soup.find(attrs={"property": prop})
        if not tag:
            return ""
        return self._clean_text(tag.get("value") or tag.get_text(" ", strip=True))

    def _about_record_value(self, soup, label):
        text = soup.get_text(" ", strip=True)
        return self._extract_labeled_text(text, label)

    def _approaching_wall_budget(self, start_ts):
        return time.time() - start_ts >= self.WALL_BUDGET_SECONDS - 75
