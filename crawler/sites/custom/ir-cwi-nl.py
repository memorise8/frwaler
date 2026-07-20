# -*- coding: utf-8 -*-
"""Crawler for the CWI institutional repository."""

from __future__ import annotations

import json
import re
import subprocess
import time
from copy import deepcopy
from datetime import datetime
from html import unescape
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class IrCwiNlCrawler(BaseCrawler):
    site_id = "ir-cwi-nl"
    site_name = "Custom: ir-cwi-nl"
    base_url = "https://ir.cwi.nl"

    _START_URL = "https://ir.cwi.nl/#facet=type:dataset"
    _SEARCH_URL = "https://ir.cwi.nl/search/query"
    _TARGET_TYPES = ("dataset",)
    _PAGE_SIZE = 10
    _MIN_ABSTRACT_CHARS = 100
    _MAX_PAGES = 200
    _MAX_WALL_SECONDS = 25 * 60

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network and parser helpers
    # ------------------------------------------------------------------

    def _curl(self, url, *, method="GET", data=None, referer=None, accept=None,
              timeout=45):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--connect-timeout",
            "15",
            "--max-time",
            str(timeout),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            f"Accept: {accept or 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'}",
            "-H",
            "Accept-Language: en-US,en;q=0.9",
        ]
        body = None
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        if method.upper() == "POST":
            body = (data or "").encode("utf-8", errors="replace")
            cmd.extend([
                "-X",
                "POST",
                "-H",
                "Content-Type: application/json",
                "-H",
                "X-Requested-With: XMLHttpRequest",
                "--data-binary",
                "@-",
            ])
        cmd.append(url)

        waits = [1, 3, 9]
        last_error = "unknown error"
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd,
                    input=body,
                    capture_output=True,
                    timeout=timeout + 10,
                    check=False,
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                if result.returncode == 0 and result.stdout:
                    return result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} stderr={stderr}"

            if attempt < 2:
                wait = waits[attempt]
                print(
                    f"[{self.site_id}] curl failed {attempt + 1}/3 for {url}: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    @staticmethod
    def _parse_html(raw):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _clean_text(value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\xa0", " ").replace("\r", "\n").replace("\f", "\n")
        text = re.sub(r"[ \t\v]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        return text.strip()

    @classmethod
    def _one_line(cls, value):
        return re.sub(r"\s+", " ", cls._clean_text(value)).strip()

    @staticmethod
    def _dedupe(items):
        seen = set()
        result = []
        for item in items:
            value = str(item).strip()
            key = value.lower()
            if value and key not in seen:
                seen.add(key)
                result.append(value)
        return result

    @classmethod
    def _tag_text(cls, tag):
        if not tag:
            return ""
        return cls._one_line(tag.get_text(" ", strip=True))

    @staticmethod
    def _parse_date(raw):
        text = str(raw or "").strip()
        if not text:
            return ""

        match = re.search(r"\d{4}-\d{2}-\d{2}", text)
        if match:
            return match.group(0)

        match = re.search(r"\d{4}/\d{1,2}/\d{1,2}", text)
        if match:
            parts = [int(p) for p in match.group(0).split("/")]
            return f"{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}"

        for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%d %B %Y", "%B %d, %Y"):
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                continue

        year = re.search(r"\b(19|20)\d{2}\b", text)
        return year.group(0) if year else ""

    # ------------------------------------------------------------------
    # Search API
    # ------------------------------------------------------------------

    def _load_initial_query(self):
        raw = self._curl(
            self.base_url + "/",
            referer=self._START_URL,
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        )
        if not raw:
            return self._fallback_query()

        soup = self._parse_html(raw)
        if soup is None:
            return self._fallback_query()

        script = soup.find("script", id="initial-query")
        if not script or not script.string:
            return self._fallback_query()

        try:
            data = json.loads(script.string)
        except (TypeError, ValueError) as exc:
            print(f"[{self.site_id}] initial-query JSON failed: {exc}")
            return self._fallback_query()

        return data

    @staticmethod
    def _fallback_query():
        return {
            "query": {
                "filters": {
                    "options": [{"field_id": "all", "title": "All Fields"}],
                    "values": [{"field_id": "all", "query": ""}],
                },
                "facets": [
                    {
                        "title": "Type",
                        "field_id": "type",
                        "api_only": False,
                        "users_only": False,
                        "type": "default",
                        "partOf_start": 0,
                        "max_terms": 100,
                        "max_display_terms": 3,
                        "sort_by": "count",
                        "sort_order": "descending",
                        "filters": [],
                    }
                ],
                "sort": "auto",
            },
            "citation": {"formats": [], "styles": []},
        }

    def _search_payload(self, initial_query, offset):
        payload = deepcopy(initial_query)
        query = payload.setdefault("query", {})
        query["from"] = offset
        query.setdefault("sort", "auto")

        facets = query.setdefault("facets", [])
        type_facet = None
        for facet in facets:
            if facet.get("field_id") == "type":
                type_facet = facet
                break
        if type_facet is None:
            type_facet = {
                "title": "Type",
                "field_id": "type",
                "api_only": False,
                "users_only": False,
                "type": "default",
                "partOf_start": 0,
                "max_terms": 100,
                "max_display_terms": 3,
                "sort_by": "count",
                "sort_order": "descending",
                "filters": [],
            }
            facets.append(type_facet)

        type_facet["filters"] = [{"term": value} for value in self._TARGET_TYPES]
        filters = query.setdefault("filters", {})
        values = filters.setdefault("values", [])
        if not values:
            values.append({"field_id": "all", "query": ""})
        return payload

    def _fetch_search_page(self, initial_query, offset):
        payload = self._search_payload(initial_query, offset)
        raw = self._curl(
            self._SEARCH_URL,
            method="POST",
            data=json.dumps(payload, ensure_ascii=False),
            referer=self._START_URL,
            accept="application/json, text/javascript, */*; q=0.01",
        )
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError) as exc:
            print(f"[{self.site_id}] search JSON parse failed at offset {offset}: {exc}")
            return None

    # ------------------------------------------------------------------
    # Detail parsing
    # ------------------------------------------------------------------

    def _meta_values(self, soup, name):
        values = []
        if soup is None:
            return values
        for meta in soup.find_all("meta", attrs={"name": name}):
            content = meta.get("content")
            if content:
                values.append(self._one_line(content))
        return values

    def _meta_value(self, soup, name):
        values = self._meta_values(soup, name)
        return values[0] if values else ""

    def _metadata_table(self, soup):
        metadata = {}
        if soup is None:
            return metadata

        for row in soup.select("#publication-metadata table tr"):
            cells = row.find_all("td", recursive=False)
            if len(cells) < 2:
                continue
            key = self._tag_text(cells[0])
            key = re.sub(r"\s+", " ", key).strip()
            if not key or key.lower() == "citation":
                continue

            values = []
            for node in cells[1].select(".publication-metadata-value, a"):
                text = self._tag_text(node)
                if text:
                    values.append(text)
            if not values:
                text = self._tag_text(cells[1])
                if text:
                    values = [text]
            values = self._dedupe(values)
            if values:
                metadata[key] = values if len(values) > 1 else values[0]

        return metadata

    def _authors_from_hit(self, hit):
        authors = []
        for author in hit.get("author") or []:
            if not isinstance(author, dict):
                continue
            label = author.get("label") or ""
            family = self._one_line(author.get("family_name"))
            given = self._one_line(author.get("given_name"))
            prefix = self._one_line(author.get("prefix"))
            if family:
                family_bits = [prefix, family] if prefix else [family]
                family_name = " ".join(family_bits)
                name = f"{family_name}, {given}" if given else family_name
            else:
                name = self._one_line(label)
            if name:
                authors.append(name)
        return self._dedupe(authors)

    def _parse_detail(self, raw, hit):
        soup = self._parse_html(raw)
        if soup is None:
            raise ValueError("detail HTML could not be parsed")

        detail_url = urljoin(self.base_url, (hit.get("url") or "").strip())
        if not detail_url:
            detail_url = f"{self.base_url}/pub/{hit.get('id')}"

        title = (
            self._meta_value(soup, "citation_title")
            or self._tag_text(soup.select_one("h1"))
            or self._one_line(hit.get("title"))
        )
        abstract = (
            self._meta_value(soup, "citation_abstract")
            or self._tag_text(soup.select_one("p.abstract"))
        )
        abstract = self._clean_text(abstract)

        authors = self._meta_values(soup, "citation_author") or self._authors_from_hit(hit)
        keywords = []
        for node in soup.select('.publication-metadata-value[key="Keywords"]'):
            text = self._tag_text(node)
            if text:
                keywords.append(text)

        detail_metadata = self._metadata_table(soup)
        if not keywords:
            raw_keywords = detail_metadata.get("Keywords")
            if isinstance(raw_keywords, list):
                keywords = raw_keywords
            elif raw_keywords:
                keywords = [raw_keywords]
        keywords = self._dedupe(keywords)

        pdf_url = self._meta_value(soup, "citation_pdf_url")
        if not pdf_url:
            pdf_link = soup.select_one('.publication-downloads a[href$=".pdf"]')
            if pdf_link:
                pdf_url = urljoin(detail_url + "/", pdf_link.get("href", ""))

        doi = self._meta_value(soup, "citation_doi") or self._one_line(hit.get("doi"))
        published_date = self._parse_date(
            self._meta_value(soup, "citation_publication_date")
            or self._meta_value(soup, "citation_date")
            or hit.get("issued")
        )

        category = self._one_line(hit.get("type"))
        department = ""
        org_value = detail_metadata.get("Organisation")
        if isinstance(org_value, list):
            department = "; ".join(org_value)
        elif org_value:
            department = str(org_value)
        if not department:
            department = self._one_line(hit.get("affiliation"))

        journal = (
            self._meta_value(soup, "citation_journal_title")
            or self._one_line(hit.get("journal_title"))
        )
        citation = self._tag_text(soup.select_one("#citation-text .csl-entry"))
        if not citation:
            citation = self._one_line(hit.get("citation"))

        metadata = {
            "source": "https://ir.cwi.nl/search/query",
            "detailEndpoint": detail_url,
            "startUrl": self._START_URL,
            "type": category,
            "journal": journal,
            "volume": self._meta_value(soup, "citation_volume") or hit.get("volume"),
            "issue": self._meta_value(soup, "citation_issue") or hit.get("issue"),
            "firstPage": self._meta_value(soup, "citation_firstpage") or hit.get("start_page"),
            "lastPage": self._meta_value(soup, "citation_lastpage") or hit.get("end_page"),
            "issn": self._meta_value(soup, "citation_issn"),
            "citation": citation,
            "openAccess": hit.get("open_access"),
            "collectionId": hit.get("collection_id"),
            "issuedLabel": hit.get("issued_label"),
            "metadataTable": detail_metadata,
            "listHit": hit,
        }

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": str(hit.get("id") or "").strip(),
            "title": title,
            "authors": json.dumps(authors, ensure_ascii=False),
            "abstract": abstract,
            "category": category,
            "keywords": json.dumps(keywords, ensure_ascii=False),
            "published_date": published_date,
            "url": detail_url,
            "pdf_url": pdf_url,
            "doi": doi,
            "department": department,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        initial_query = self._load_initial_query()
        offset = 0
        saved = 0
        total = None
        seen_urls: set = set()
        start_time = time.time()
        page_num = 0
        limit_label = str(limit) if limit is not None else "inf"

        while page_num < self._MAX_PAGES:
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time > self._MAX_WALL_SECONDS:
                print(f"[{self.site_id}] 25-minute budget reached at page {page_num + 1}; stopping")
                break

            time.sleep(self._delay)
            data = self._fetch_search_page(initial_query, offset)
            if data is None:
                print(f"[{self.site_id}] search page at offset {offset} failed; stopping")
                break

            hits = data.get("hits") or []
            if total is None:
                total = data.get("query", {}).get("total")
                if total is not None:
                    print(f"[{self.site_id}] Total matching records: {total}")

            if not hits:
                print(f"[{self.site_id}] No more hits at offset {offset}; stopping")
                break

            page_num += 1
            if page_num % 10 == 1:
                print(f"[{self.site_id}] page {page_num}: saved {saved}/{limit_label}")

            for item_no, hit in enumerate(hits, start=offset + 1):
                if limit is not None and saved >= limit:
                    break

                external_id = str(hit.get("id") or "").strip()
                label = external_id or str(item_no)
                try:
                    category = self._one_line(hit.get("type"))
                    if category and category not in self._TARGET_TYPES:
                        print(f"[{self.site_id}] item {label} skipped: type {category!r}")
                        continue
                    if not external_id:
                        print(f"[{self.site_id}] item {label} skipped: missing id")
                        continue

                    detail_url = urljoin(self.base_url, hit.get("url") or f"/pub/{external_id}")
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)

                    time.sleep(self.detail_delay)
                    raw = self._curl(
                        detail_url,
                        referer=self._START_URL,
                        accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    )
                    if not raw:
                        print(f"[{self.site_id}] item {label} skipped: detail fetch failed")
                        continue

                    paper = self._parse_detail(raw, hit)
                    if not paper.get("title"):
                        print(f"[{self.site_id}] item {label} skipped: missing title")
                        continue
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self._MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {label} skipped: "
                            f"short abstract ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(f"[{self.site_id}] Saved {counter}: {paper['title'][:80]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {label} failed: {exc}")
                    continue

            offset += max(len(hits), self._PAGE_SIZE)
            if total is not None and offset >= int(total):
                print(f"[{self.site_id}] all {total} records exhausted; done")
                break

        else:
            print(f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached; stopping")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
