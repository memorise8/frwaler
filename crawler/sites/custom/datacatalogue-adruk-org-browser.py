# -*- coding: utf-8 -*-
"""ADR UK Metadata Catalogue browser crawler."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime
from html import unescape
from typing import Any

from bs4 import BeautifulSoup

# spec_from_file_location has no package context; keep imports absolute.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from crawler.base_crawler import BaseCrawler


class DatacatalogueAdrukOrgBrowserCrawler(BaseCrawler):
    site_id = "datacatalogue-adruk-org-browser"
    site_name = "Custom: datacatalogue-adruk-org-browser"
    base_url = "https://datacatalogue.adruk.org"
    DELIVERY_ORDER = "arbitrary"

    START_URL = (
        "https://datacatalogue.adruk.org/browser/search"
        "?include=dataset::datastandard::terminology::dataclass::dataelement"
    )
    API_BASE_URL = "https://api-datacatalogue.adruk.org/api"
    LIST_ENDPOINT = "dataset"
    INCLUDE = "dataset::datastandard::terminology::dataclass::dataelement"
    PAGE_SIZE = 100
    MAX_PAGES = 200
    WALL_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    BACKOFF_SECONDS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 50

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.monotonic()
        limit_or_inf = str(limit) if limit is not None else "inf"
        reached_cap = True

        if limit is not None and limit <= 0:
            print(f"[{self.site_id}] done. Total saved: 0")
            return 0

        page_start = (self.delivery_cursor or {}).get("page", 1)
        pages_attempted = 0
        # MAX_PAGES is a per-run chunk size (not an absolute ceiling) so a resume
        # from a large cursor still walks a full budget of pages this run.
        for page in range(page_start, page_start + self.MAX_PAGES):
            if limit is not None and saved >= limit:
                reached_cap = False
                break

            elapsed = time.monotonic() - start_time
            if elapsed >= self.WALL_BUDGET_SECONDS - 15:
                print(
                    f"[{self.site_id}] wall-clock budget approaching "
                    f"({elapsed:.0f}s); exiting cleanly"
                )
                reached_cap = False
                break

            pages_attempted += 1

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            list_data = self._fetch_list_page(page)
            if not list_data:
                if page == 1:
                    list_data = self._fetch_initial_html_list()
                if not list_data:
                    print(f"[{self.site_id}] list page {page} failed after retries; stopping")
                    reached_cap = False
                    break

            records = list_data.get("content") or []
            if not records:
                print(f"[{self.site_id}] page {page} returned 0 records; stopping")
                reached_cap = False
                break

            if page == 1:
                total = list_data.get("totalCount")
                total_pages = list_data.get("totalPages")
                if total is not None:
                    print(f"[{self.site_id}] total records on server: {total}")
                if total_pages is not None:
                    print(f"[{self.site_id}] total pages on server: {total_pages}")

            page_had_unseen = False
            for offset, item in enumerate(records, start=1):
                if limit is not None and saved >= limit:
                    break

                item_label = item.get("id") or f"page {page} item {offset}"
                try:
                    dataset_id = self._string(item.get("id"))
                    origin_id = self._origin_id(item)
                    if not dataset_id:
                        raise RuntimeError("record has no dataset id")

                    detail_url = self._detail_page_url(dataset_id, origin_id)
                    url_key = detail_url or f"{dataset_id}:{origin_id}"
                    if url_key in seen_urls:
                        continue
                    seen_urls.add(url_key)
                    page_had_unseen = True

                    time.sleep(getattr(self, "detail_delay", self._delay))

                    detail = self._fetch_detail_json(dataset_id, origin_id)
                    if detail is None:
                        detail = self._fetch_detail_html(dataset_id, origin_id)
                    if detail is None:
                        raise RuntimeError("detail fetch failed after retries")

                    paper = self._parse_record(item, detail, detail_url)
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:90]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            self._advance_cursor({"page": page + 1}, items_done=len(records))

            if not page_had_unseen:
                print(f"[{self.site_id}] page {page} had no new URLs; stopping")
                reached_cap = False
                break

            try:
                total_pages = int(list_data.get("totalPages") or 0)
            except (TypeError, ValueError):
                total_pages = 0
            if total_pages and page >= total_pages:
                reached_cap = False
                break

        if reached_cap and pages_attempted > 0:
            print(f"[{self.site_id}] reached safety cap of {self.MAX_PAGES} pages this run")

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page: int) -> dict[str, Any] | None:
        params = {
            "include": self.INCLUDE,
            "pageNumber": str(page),
            "pageSize": str(self.PAGE_SIZE),
        }
        return self._curl_json(
            f"{self.API_BASE_URL}/{self.LIST_ENDPOINT}?{urllib.parse.urlencode(params)}",
            context=f"list page {page}",
        )

    def _fetch_detail_json(self, dataset_id: str, origin_id: str | None) -> dict[str, Any] | None:
        params = {}
        if origin_id:
            params["originId"] = origin_id
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        return self._curl_json(
            f"{self.API_BASE_URL}/dataset/{urllib.parse.quote(dataset_id)}{query}",
            context=f"item {dataset_id} detail api",
        )

    def _fetch_detail_html(self, dataset_id: str, origin_id: str | None) -> dict[str, Any] | None:
        raw = self._curl_get(
            self._detail_page_url(dataset_id, origin_id),
            context=f"item {dataset_id} detail html",
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        )
        if not raw:
            return None
        next_data = self._extract_next_data(raw)
        page_props = (
            next_data.get("props", {}).get("pageProps", {})
            if isinstance(next_data, dict)
            else {}
        )
        dataset = page_props.get("dataset")
        return dataset if isinstance(dataset, dict) else None

    def _fetch_initial_html_list(self) -> dict[str, Any] | None:
        raw = self._curl_get(
            self.START_URL,
            context="initial search html",
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        )
        if not raw:
            return None
        next_data = self._extract_next_data(raw)
        page_props = (
            next_data.get("props", {}).get("pageProps", {})
            if isinstance(next_data, dict)
            else {}
        )
        search_result = page_props.get("searchResult")
        return search_result if isinstance(search_result, dict) else None

    def _curl_json(self, url: str, context: str) -> dict[str, Any] | None:
        raw = self._curl_get(url, context=context, accept="application/json,*/*;q=0.8")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] {context} invalid JSON: {exc}")
            return None
        if not isinstance(data, dict):
            print(f"[{self.site_id}] {context} JSON root is not an object")
            return None
        if data.get("message") == "Page Not Found":
            print(f"[{self.site_id}] {context} returned Page Not Found")
            return None
        return data

    def _curl_get(self, url: str, context: str, accept: str) -> str | None:
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
            "Accept-Language: en-GB,en;q=0.9",
            "-H",
            "X-API-Version: 2.0",
            url,
        ]

        last_error = ""
        for attempt, wait in enumerate(self.BACKOFF_SECONDS, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=55)
                stdout = result.stdout or b""
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                if result.returncode == 0 and stdout.strip():
                    return stdout.decode("utf-8", errors="replace")
                last_error = stderr or f"curl exit {result.returncode}; empty response"
            except subprocess.TimeoutExpired as exc:
                last_error = f"curl timeout after {exc.timeout}s"
            except Exception as exc:
                last_error = str(exc)

            print(f"[{self.site_id}] {context} curl failed (attempt {attempt}/3): {last_error}")
            if attempt < 3:
                print(f"[{self.site_id}] retrying in {wait}s...")
                time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_record(self, item: dict[str, Any], detail: dict[str, Any], detail_url: str) -> dict[str, Any]:
        dataset_id = self._string(detail.get("id") or item.get("id"))
        origin = detail.get("origin") if isinstance(detail.get("origin"), dict) else item.get("origin") or {}
        origin_id = self._string(origin.get("id") if isinstance(origin, dict) else None) or self._origin_id(item)
        external_id = f"{dataset_id}:{origin_id}" if origin_id else dataset_id

        summary = detail.get("summary") if isinstance(detail.get("summary"), dict) else {}
        documentation = detail.get("documentation") if isinstance(detail.get("documentation"), dict) else {}
        access = detail.get("accessAndGovernance") if isinstance(detail.get("accessAndGovernance"), dict) else {}
        usage = access.get("usage") if isinstance(access.get("usage"), dict) else {}
        access_detail = access.get("access") if isinstance(access.get("access"), dict) else {}

        title = self._clean_text(summary.get("title") or item.get("title") or "")
        if not title:
            raise RuntimeError(f"{external_id} has no title")

        abstract_parts = []
        for part in (summary.get("abstract"), item.get("abstract"), documentation.get("description")):
            cleaned_part = self._clean_text(part)
            if cleaned_part and cleaned_part not in abstract_parts:
                abstract_parts.append(cleaned_part)
        abstract = "\n\n".join(abstract_parts)

        published_raw = (
            detail.get("issued")
            or summary.get("publicationDate")
            or self._nested(detail, ("rawData", "required", "issued"))
            or item.get("issued")
        )
        listed_raw = item.get("modified") or detail.get("lastModified") or item.get("issued") or published_raw
        published_date = self._iso_date(published_raw)
        listed_date = self._iso_date(listed_raw)

        publisher = self._publisher(summary.get("publisher")) or self._clean_text(item.get("publisher"))
        if not publisher and isinstance(origin, dict):
            publisher = self._clean_text(origin.get("name"))

        resource_creator = usage.get("resourceCreator")
        authors = self._join_people(resource_creator)
        data_controller = self._join_people(access_detail.get("dataController"))
        department = self._clean_text(origin.get("name")) if isinstance(origin, dict) else ""
        if data_controller and data_controller != publisher:
            department = data_controller

        keywords = self._keywords(summary.get("keywords") or item.get("keywords"))
        category = (
            self._clean_text(self._nested(detail, ("rawData", "summary", "datasetType")))
            or self._clean_text(detail.get("dataModelType") or item.get("dataModelType") or item.get("searchResultType"))
        )
        doi = self._clean_text(
            summary.get("doiName")
            or summary.get("doiname")
            or self._nested(detail, ("rawData", "summary", "doiName"))
            or self._nested(detail, ("rawData", "summary", "doiname"))
        )

        pdf_url, original_filename = self._extract_pdf(detail, origin_id)

        metadata = {
            "posted_date": listed_raw,
            "originalFilename": original_filename,
            "journal_raw": self._nested(detail, ("journal",)),
            "series": self._nested(detail, ("series",)),
            "volume": self._nested(detail, ("volume",)),
            "issue": self._nested(detail, ("issue",)),
            "node_id": dataset_id,
            "dataset_id": dataset_id,
            "origin_id": origin_id,
            "native_external_id": external_id,
            "issued_raw": published_raw,
            "modified_raw": listed_raw,
            "lastModified": detail.get("lastModified"),
            "status": detail.get("status"),
            "dataModelType": detail.get("dataModelType"),
            "descriptiveSchema": detail.get("descriptiveSchema"),
            "hasFiles": detail.get("hasFiles"),
            "searchResultType": item.get("searchResultType"),
            "searchDetails": item.get("searchDetails"),
            "origin": origin,
            "assets": detail.get("assets"),
            "associatedMedia": documentation.get("associatedMedia"),
            "versionHistory": detail.get("versionHistory"),
            "list_record": item,
            "detail_record": detail,
        }
        metadata = {k: v for k, v in metadata.items() if v not in (None, "", [], {})}

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": dataset_id or external_id or None,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": authors or None,
            "publisher": publisher or None,
            "department": department or None,
            "journal": None,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": keywords or None,
            "category": category or None,
            "doi": doi or None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _extract_next_data(self, raw: str) -> dict[str, Any]:
        soup = self._make_soup(raw)
        if soup is None:
            return {}
        script = soup.find("script", id="__NEXT_DATA__", type="application/json")
        if script is None:
            script = soup.find("script", id="__NEXT_DATA__")
        if script is None:
            return {}
        try:
            return json.loads(script.get_text() or "{}")
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] __NEXT_DATA__ JSON parse failed: {exc}")
            return {}

    def _make_soup(self, raw: str):
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                last_exc = exc
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed: {exc}")
        print(f"[{self.site_id}] all BeautifulSoup parsers failed: {last_exc}")
        return None

    # ------------------------------------------------------------------
    # Field helpers
    # ------------------------------------------------------------------

    def _detail_page_url(self, dataset_id: str, origin_id: str | None) -> str:
        dataset_part = urllib.parse.quote(str(dataset_id).strip())
        if origin_id:
            return f"{self.base_url}/browser/dataset/{dataset_part}/{urllib.parse.quote(str(origin_id))}"
        return f"{self.base_url}/browser/dataset/{dataset_part}"

    @staticmethod
    def _origin_id(item: dict[str, Any]) -> str | None:
        origin = item.get("origin") if isinstance(item.get("origin"), dict) else {}
        value = item.get("originId") or origin.get("id")
        value = str(value).strip() if value is not None else ""
        return value or None

    @staticmethod
    def _string(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        return str(value).strip()

    @classmethod
    def _clean_text(cls, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (list, tuple, set)):
            value = " ".join(cls._clean_text(v) for v in value if v is not None)
        elif isinstance(value, dict):
            value = " ".join(cls._clean_text(v) for v in value.values() if v is not None)
        else:
            value = str(value)

        text = value
        for _ in range(4):
            new_text = unescape(text)
            if new_text == text:
                break
            text = new_text
        if "<" in text and ">" in text:
            text = re.sub(r"<[^>]+>", " ", text)
        text = text.replace("\xa0", " ")
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        return text.strip()

    @classmethod
    def _keywords(cls, value: Any) -> str:
        values = cls._listify(value)
        split_values: list[str] = []
        for value in values:
            if ";,;" in value:
                split_values.extend(value.split(";,;"))
            elif isinstance(value, str) and value.startswith("[") and value.endswith("]"):
                try:
                    parsed = json.loads(value)
                    split_values.extend(cls._listify(parsed))
                except (TypeError, ValueError):
                    split_values.append(value)
            else:
                split_values.append(value)
        cleaned = cls._dedupe(cls._clean_text(v).strip(" ;,") for v in split_values)
        return ",".join(cleaned)

    @classmethod
    def _join_people(cls, value: Any) -> str:
        values = cls._listify(value)
        names = []
        for value in values:
            if isinstance(value, str) and ";,;" in value:
                names.extend(value.split(";,;"))
            else:
                names.append(value)
        cleaned = cls._dedupe(cls._clean_text(v).strip(" ;,") for v in names)
        return "; ".join(cleaned)

    @classmethod
    def _publisher(cls, value: Any) -> str:
        if isinstance(value, dict):
            return cls._clean_text(value.get("name") or value.get("title") or value.get("identifier"))
        return cls._join_people(value)

    @classmethod
    def _listify(cls, value: Any) -> list[Any]:
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        if isinstance(value, dict):
            name = value.get("name") or value.get("title") or value.get("label")
            return [name] if name else list(value.values())
        return [value]

    @staticmethod
    def _dedupe(values) -> list[str]:
        seen = set()
        out = []
        for value in values:
            value = str(value).strip()
            if not value or value in seen:
                continue
            seen.add(value)
            out.append(value)
        return out

    @staticmethod
    def _nested(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
        cur: Any = data
        for key in keys:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(key)
        return cur

    def _extract_pdf(self, detail: dict[str, Any], origin_id: str | None) -> tuple[str | None, str | None]:
        assets = detail.get("assets") if isinstance(detail.get("assets"), dict) else {}
        files = assets.get("files") if isinstance(assets.get("files"), list) else []
        for file_info in files:
            if not isinstance(file_info, dict):
                continue
            name = self._clean_text(file_info.get("name"))
            asset_id = self._string(file_info.get("id"))
            if name.lower().endswith(".pdf") or "pdf" in name.lower():
                if asset_id:
                    query = f"?originId={urllib.parse.quote(str(origin_id))}" if origin_id else ""
                    return f"{self.API_BASE_URL}/asset/{urllib.parse.quote(asset_id)}{query}", name or None

        documentation = detail.get("documentation") if isinstance(detail.get("documentation"), dict) else {}
        for media in self._listify(documentation.get("associatedMedia")):
            url = ""
            if isinstance(media, dict):
                url = self._string(media.get("url") or media.get("name"))
            else:
                url = self._string(media)
            if ".pdf" in url.lower():
                return url, self._filename_from_url(url)
        return None, None

    @staticmethod
    def _filename_from_url(url: str | None) -> str | None:
        if not url:
            return None
        path = urllib.parse.urlparse(url).path
        tail = urllib.parse.unquote(path.rstrip("/").split("/")[-1])
        return tail if tail and "." in tail and len(tail) <= 255 else None

    @classmethod
    def _iso_date(cls, value: Any) -> str | None:
        raw = cls._clean_text(value)
        if not raw:
            return None

        match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", raw)
        if match:
            y, m, d = match.groups()
            return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"

        match = re.search(r"\b(\d{4})(\d{2})(\d{2})\b", raw)
        if match:
            y, m, d = match.groups()
            return f"{y}-{m}-{d}"

        for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(raw[:32], fmt).date().isoformat()
            except ValueError:
                pass

        iso_candidate = raw.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(iso_candidate).date().isoformat()
        except ValueError:
            return None
