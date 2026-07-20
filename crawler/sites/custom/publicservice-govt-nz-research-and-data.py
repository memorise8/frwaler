# -*- coding: utf-8 -*-
"""Crawler for Public Service Commission research/data datasheets.

The filtered listing is rendered by Silverstripe at ``/research-and-data``.
Pagination uses a ``start`` offset in increments of 25.  Individual records
are either HTML detail pages under ``/research-and-data/...`` or direct PDF
assets under ``/assets/DirectoryFile/...``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import time
import uuid
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class PublicServiceGovtNzResearchAndDataCrawler(BaseCrawler):
    site_id = "publicservice-govt-nz-research-and-data"
    site_name = "Custom: publicservice-govt-nz-research-and-data"
    base_url = "https://www.publicservice.govt.nz"

    START_URL = (
        "https://www.publicservice.govt.nz/research-and-data"
        "?q=&classification=Datasheet&topics%5B0%5D=193"
    )
    LIST_ENDPOINT = f"{base_url}/research-and-data"
    PAGE_SIZE = 25
    MAX_PAGES = 200
    MAX_RUNTIME_SECONDS = 25 * 60
    RUNTIME_GRACE_SECONDS = 30
    DEFAULT_DEPARTMENT = "Te Kawa Mataaho Public Service Commission"

    # ------------------------------------------------------------------
    # Fetching / parsing helpers
    # ------------------------------------------------------------------

    def _curl_get(
        self,
        url: str,
        *,
        timeout: int = 45,
        accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        head: bool = False,
    ) -> Optional[str]:
        """Fetch with curl and retry transient failures with 1s/3s backoff."""
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--max-time",
            str(timeout),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            f"Accept: {accept}",
        ]
        if head:
            cmd.append("-I")
        cmd.append(url)

        last_error = ""
        waits = [1, 3, 9]
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 5,
                    check=False,
                )
                body = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and (body.strip() or head):
                    return body
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = stderr or f"curl exit {result.returncode}"
            except subprocess.TimeoutExpired as exc:
                last_error = f"timeout after {exc.timeout}s"
            except OSError as exc:
                last_error = str(exc)

            if attempt < 2:
                wait = waits[attempt]
                print(
                    f"[{self.site_id}] fetch failed for {url} "
                    f"(attempt {attempt + 1}/3): {last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] fetch failed after 3 attempts for {url}: {last_error}")
        return None

    def _make_soup(self, raw: str) -> BeautifulSoup:
        """Construct BeautifulSoup with a tolerant parser fallback chain."""
        last_error = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                last_error = exc
                print(f"[{self.site_id}] BeautifulSoup parser {parser} failed: {exc}")
        print(f"[{self.site_id}] all HTML parsers failed: {last_error}")
        return BeautifulSoup("", "html.parser")

    @staticmethod
    def _clean_text(value: str) -> str:
        value = (value or "").replace("\xa0", " ")
        value = re.sub(r"\s+", " ", value)
        return value.strip()

    @staticmethod
    def _dedupe_text(parts: Iterable[str]) -> List[str]:
        deduped: List[str] = []
        seen = set()
        for part in parts:
            cleaned = PublicServiceGovtNzResearchAndDataCrawler._clean_text(part)
            if not cleaned:
                continue
            key = cleaned.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(cleaned)
        return deduped

    def _absolute_url(self, href: str) -> str:
        return urljoin(self.base_url, href or "")

    @staticmethod
    def _strip_tracking_params(url: str) -> str:
        parts = urlsplit(url)
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key != "_searchAnalytics"
        ]
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path.rstrip("/"), urlencode(query), "")
        )

    @staticmethod
    def _is_pdf_url(url: str) -> bool:
        return urlsplit(url).path.lower().endswith(".pdf")

    def _list_url(self, page: int) -> str:
        start = max(page - 1, 0) * self.PAGE_SIZE
        params = [
            ("q", ""),
            ("classification", "datasheet"),
            ("topics[0]", "193"),
        ]
        if start:
            params.append(("start", str(start)))
        return f"{self.LIST_ENDPOINT}?{urlencode(params)}"

    @staticmethod
    def _decode_search_analytics(url: str) -> Dict[str, Any]:
        query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
        raw = query.get("_searchAnalytics")
        if not raw:
            return {}
        try:
            padded = raw + "=" * (-len(raw) % 4)
            data = base64.urlsafe_b64decode(padded.encode("ascii"))
            parsed = json.loads(data.decode("utf-8", errors="replace"))
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _parse_date(value: Optional[str]) -> str:
        if not value:
            return ""
        value = PublicServiceGovtNzResearchAndDataCrawler._clean_text(value)
        value = value.replace(",", "")
        if not value:
            return ""

        for fmt in (
            "%d %B %Y",
            "%d %b %Y",
            "%B %Y",
            "%b %Y",
            "%Y-%m-%d",
            "%Y/%m/%d",
        ):
            try:
                dt = datetime.strptime(value, fmt)
                return dt.date().isoformat()
            except ValueError:
                pass

        try:
            return parsedate_to_datetime(value).date().isoformat()
        except Exception:
            pass

        match = re.search(r"\d{4}-\d{2}-\d{2}", value)
        return match.group(0) if match else ""

    def _parse_list_page(self, raw: str, list_url: str) -> Tuple[List[Dict[str, Any]], str]:
        soup = self._make_soup(raw)
        container = soup.select_one(".result-list")
        if not container:
            return [], ""

        nodes = container.find_all("div", class_="result", recursive=False)
        if not nodes:
            nodes = container.select(".result")

        records: List[Dict[str, Any]] = []
        for node in nodes:
            link = node.select_one("a.result__title-link[href]")
            title_node = node.select_one(".result__title")
            if not link or not title_node:
                continue

            raw_url = self._absolute_url(link.get("href") or "")
            canonical_url = self._strip_tracking_params(raw_url)
            title = self._clean_text(title_node.get_text(" "))
            if not canonical_url or not title:
                continue

            date_node = node.select_one(".result__date")
            desc_node = node.select_one('p[id$="-metaDescription"]')
            record_id = ""
            if desc_node and desc_node.get("id"):
                record_id = re.sub(r"-metaDescription$", "", desc_node["id"])

            categories = [
                self._clean_text(item.get_text(" "))
                for item in node.select(".result__taxon-individual")
            ]
            categories = [item for item in categories if item]

            downloads = []
            for download in node.select(".result__actions a[href]"):
                href = self._absolute_url(download.get("href") or "")
                href = self._strip_tracking_params(href)
                label = self._clean_text(download.get_text(" "))
                if href:
                    downloads.append({"url": href, "label": label})

            pdf_url = canonical_url if self._is_pdf_url(canonical_url) else ""
            if not pdf_url:
                for download in downloads:
                    if self._is_pdf_url(download["url"]):
                        pdf_url = download["url"]
                        break

            analytics = self._decode_search_analytics(raw_url)
            if not record_id:
                document_id = str(analytics.get("documentId") or "")
                match = re.search(r"_(\d+)$", document_id)
                if match:
                    record_id = match.group(1)

            records.append(
                {
                    "title": title,
                    "url": canonical_url,
                    "raw_url": raw_url,
                    "date_raw": self._clean_text(date_node.get_text(" ")) if date_node else "",
                    "abstract": self._clean_text(desc_node.get_text(" ")) if desc_node else "",
                    "record_id": record_id,
                    "categories": categories,
                    "pdf_url": pdf_url,
                    "downloads": downloads,
                    "list_url": list_url,
                    "search_analytics": analytics,
                    "detail_kind": "pdf" if self._is_pdf_url(canonical_url) else "html",
                }
            )

        next_url = ""
        pagination = soup.select_one(".pagination")
        if pagination:
            for link in pagination.select("a[href]"):
                classes = link.get("class") or []
                title = (link.get("title") or "").lower()
                if "pagination__end" in classes or "next" in title:
                    next_url = self._absolute_url(link.get("href") or "")
                    break

        return records, next_url

    def _extract_title(self, soup: BeautifulSoup, fallback: str) -> str:
        for selector in ("main h1 .title--en", "main h1", 'meta[property="og:title"]', "title"):
            node = soup.select_one(selector)
            if not node:
                continue
            if node.name == "meta":
                text = self._clean_text(node.get("content") or "")
            else:
                text = self._clean_text(node.get_text(" "))
            if text:
                return re.split(r"\s+\|\s+|\s+-\s+Te Kawa", text)[0].strip()
        return fallback

    def _extract_breadcrumbs(self, soup: BeautifulSoup) -> List[str]:
        crumbs = []
        for link in soup.select(".banner__breadcrumbs a"):
            text = self._clean_text(link.get_text(" "))
            if text:
                crumbs.append(text)
        return crumbs

    def _extract_labeled_fields(self, soup: BeautifulSoup) -> Dict[str, str]:
        labels = ("Authors", "Publisher", "Format", "Date published")
        label_pattern = "|".join(re.escape(label) for label in labels)
        fields: Dict[str, str] = {}

        for node in soup.select(".content-element__content"):
            text = self._clean_text(node.get_text(" "))
            if not any(f"{label}:" in text for label in labels):
                continue
            pattern = rf"({label_pattern}):\s*(.*?)(?=(?:{label_pattern}):|$)"
            for match in re.finditer(pattern, text):
                key = match.group(1)
                value = self._clean_text(match.group(2))
                if value:
                    fields[key] = value
            if fields:
                break

        return fields

    @staticmethod
    def _split_authors(value: str) -> List[str]:
        if not value:
            return []
        value = re.sub(r"\s+\band\b\s+", ", ", value)
        parts = re.split(r"\s*[,;]\s*", value)
        return [part.strip() for part in parts if part.strip()]

    def _extract_detail_date(self, soup: BeautifulSoup, fields: Dict[str, str], fallback: str) -> str:
        main = soup.find("main") or soup
        meta_node = main.select_one(".meta")
        if meta_node:
            parsed = self._parse_date(meta_node.get_text(" "))
            if parsed:
                return parsed
        parsed = self._parse_date(fields.get("Date published"))
        if parsed:
            return parsed
        return self._parse_date(fallback)

    def _extract_detail_abstract(self, soup: BeautifulSoup, fallback: str) -> str:
        parts: List[str] = []
        main = soup.find("main") or soup

        intro = main.select_one(".body__intro")
        if intro:
            text = self._clean_text(intro.get_text(" "))
            if text:
                parts.append(text)

        for section in main.select("section.element"):
            heading = self._clean_text(section.select_one("h2").get_text(" ")) if section.select_one("h2") else ""
            if "abstract" not in heading.lower():
                continue
            content = section.select_one(".content-element__content") or section
            for child in content.find_all(["p", "li"], recursive=True):
                text = self._clean_text(child.get_text(" "))
                if text:
                    parts.append(text)

        if len(" ".join(parts)) < 100:
            desc = soup.find("meta", attrs={"name": "description"})
            if desc and desc.get("content"):
                parts.insert(0, self._clean_text(desc["content"]))

        if len(" ".join(parts)) < 100 and fallback:
            parts.insert(0, fallback)

        if len(" ".join(parts)) < 100:
            for node in main.select(".content-element__content p"):
                text = self._clean_text(node.get_text(" "))
                if not text or re.search(r"^(Authors|Publisher|Format|Date published):", text):
                    continue
                parts.append(text)
                if len(" ".join(parts)) >= 500:
                    break

        return self._clean_text(" ".join(self._dedupe_text(parts)))

    def _extract_pdf_url(self, soup: BeautifulSoup, fallback: str) -> str:
        if fallback:
            return fallback
        main = soup.find("main") or soup
        for link in main.find_all("a", href=True):
            href = self._strip_tracking_params(self._absolute_url(link.get("href") or ""))
            if self._is_pdf_url(href):
                return href
        return ""

    def _external_id(self, record: Dict[str, Any], canonical_url: str) -> str:
        if record.get("record_id"):
            return f"publicservice-{record['record_id']}"
        return hashlib.sha1(canonical_url.encode("utf-8")).hexdigest()

    def _build_keywords(
        self,
        categories: Iterable[str],
        fields: Optional[Dict[str, str]] = None,
        is_pdf: bool = False,
    ) -> List[str]:
        values = ["research", "datasheet"]
        values.extend(categories or [])
        if is_pdf:
            values.append("pdf")
        if fields and fields.get("Format"):
            values.append(fields["Format"])

        keywords = []
        seen = set()
        for value in values:
            cleaned = self._clean_text(str(value))
            key = cleaned.lower()
            if cleaned and key not in seen:
                seen.add(key)
                keywords.append(cleaned)
        return keywords

    def _parse_pdf_record(
        self,
        record: Dict[str, Any],
        headers: str,
    ) -> Optional[Dict[str, Any]]:
        canonical = record["url"]
        abstract = self._clean_text(record.get("abstract") or "")
        if len(abstract) < 50:
            print(
                f"[{self.site_id}] short abstract for {canonical} "
                f"({len(abstract)} chars); skipping"
            )
            return None

        metadata = {
            "source": "publicservice.govt.nz server-rendered listing",
            "list_endpoint": record.get("list_url"),
            "detail_endpoint": canonical,
            "detail_kind": "pdf",
            "head_headers_sample": "\n".join((headers or "").splitlines()[:20]),
            "downloads": record.get("downloads", []),
            "search_analytics": record.get("search_analytics", {}),
        }
        category = " > ".join(record.get("categories") or []) or "Research"

        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, canonical)),
            "site_id": self.site_id,
            "external_id": self._external_id(record, canonical),
            "title": record["title"],
            "authors": json.dumps([], ensure_ascii=False),
            "abstract": abstract,
            "category": category,
            "keywords": json.dumps(
                self._build_keywords(record.get("categories", []), is_pdf=True),
                ensure_ascii=False,
            ),
            "published_date": self._parse_date(record.get("date_raw")),
            "url": canonical,
            "pdf_url": canonical,
            "doi": "",
            "department": self.DEFAULT_DEPARTMENT,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _parse_html_detail(
        self,
        record: Dict[str, Any],
        raw: str,
    ) -> Optional[Dict[str, Any]]:
        soup = self._make_soup(raw)
        canonical_node = soup.find("link", attrs={"rel": "canonical"})
        canonical = (
            self._absolute_url(canonical_node.get("href") or "")
            if canonical_node
            else record["url"]
        )
        canonical = self._strip_tracking_params(canonical or record["url"])

        fields = self._extract_labeled_fields(soup)
        title = self._extract_title(soup, record["title"])
        abstract = self._extract_detail_abstract(soup, record.get("abstract", ""))
        if not title:
            print(f"[{self.site_id}] missing title for {record['url']}; skipping")
            return None
        if len(abstract) < 50:
            print(
                f"[{self.site_id}] short abstract for {canonical} "
                f"({len(abstract)} chars); skipping"
            )
            return None

        breadcrumbs = self._extract_breadcrumbs(soup)
        categories = breadcrumbs or record.get("categories") or ["Research"]
        authors = self._split_authors(fields.get("Authors", ""))
        pdf_url = self._extract_pdf_url(soup, record.get("pdf_url") or "")
        department = fields.get("Publisher") or self.DEFAULT_DEPARTMENT
        published_date = self._extract_detail_date(soup, fields, record.get("date_raw", ""))
        keywords = self._build_keywords(categories, fields=fields, is_pdf=bool(pdf_url))

        metadata = {
            "source": "publicservice.govt.nz server-rendered listing + HTML detail",
            "list_endpoint": record.get("list_url"),
            "detail_endpoint": record.get("url"),
            "canonical_url": canonical,
            "detail_kind": "html",
            "record_id": record.get("record_id"),
            "list_date_raw": record.get("date_raw"),
            "list_abstract": record.get("abstract"),
            "breadcrumbs": breadcrumbs,
            "downloads": record.get("downloads", []),
            "fields": fields,
            "search_analytics": record.get("search_analytics", {}),
        }
        if fields.get("Format"):
            metadata["journal"] = fields["Format"]

        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, canonical)),
            "site_id": self.site_id,
            "external_id": self._external_id(record, canonical),
            "title": title,
            "authors": json.dumps(authors, ensure_ascii=False),
            "abstract": abstract,
            "category": " > ".join(categories),
            "keywords": json.dumps(keywords, ensure_ascii=False),
            "published_date": published_date,
            "url": canonical,
            "pdf_url": pdf_url,
            "doi": "",
            "department": department,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _fetch_and_parse_item(self, record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        url = record["url"]
        if record.get("detail_kind") == "pdf" or self._is_pdf_url(url):
            headers = self._curl_get(
                url,
                timeout=30,
                accept="application/pdf,*/*;q=0.8",
                head=True,
            )
            if headers is None:
                print(f"[{self.site_id}] item detail failed for {url}: empty response")
                return None
            return self._parse_pdf_record(record, headers)

        raw = self._curl_get(url, timeout=45)
        if not raw:
            print(f"[{self.site_id}] item detail failed for {url}: empty response")
            return None
        return self._parse_html_detail(record, raw)

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls = set()
        limit_or_inf = limit if limit is not None else "inf"
        started = time.monotonic()

        while page <= self.MAX_PAGES:
            if limit is not None and saved >= limit:
                break

            elapsed = time.monotonic() - started
            if elapsed >= self.MAX_RUNTIME_SECONDS - self.RUNTIME_GRACE_SECONDS:
                print(f"[{self.site_id}] approaching 25 minute budget; exiting cleanly")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            list_url = self._list_url(page)
            raw = self._curl_get(list_url, timeout=45)
            if not raw:
                print(f"[{self.site_id}] page {page}: empty list response; stopping")
                break

            records, next_url = self._parse_list_page(raw, list_url)
            if not records:
                print(f"[{self.site_id}] page {page}: 0 records; stopping")
                break

            new_records: List[Dict[str, Any]] = []
            for record in records:
                url_key = record["url"].rstrip("/")
                if not url_key or url_key in seen_urls:
                    continue
                seen_urls.add(url_key)
                record["url"] = url_key
                new_records.append(record)

            if not new_records:
                print(f"[{self.site_id}] page {page}: 0 new records; stopping")
                break

            for idx, record in enumerate(new_records, start=1):
                if limit is not None and saved >= limit:
                    break

                elapsed = time.monotonic() - started
                if elapsed >= self.MAX_RUNTIME_SECONDS - self.RUNTIME_GRACE_SECONDS:
                    print(f"[{self.site_id}] approaching 25 minute budget; exiting cleanly")
                    return saved

                try:
                    time.sleep(self._delay)
                    paper = self._fetch_and_parse_item(record)
                    if not paper:
                        continue
                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit_or_inf}"
                    print(f"[{self.site_id}] saved {counter}: {paper['title'][:80]}")
                except Exception as exc:
                    print(f"[{self.site_id}] item {idx} failed: {exc}")
                    continue

            if not next_url:
                print(f"[{self.site_id}] page {page}: next page link absent; stopping")
                break

            page += 1

        if page > self.MAX_PAGES:
            print(f"[{self.site_id}] reached safety cap of {self.MAX_PAGES} pages")

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved
