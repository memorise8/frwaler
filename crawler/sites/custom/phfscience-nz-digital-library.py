# -*- coding: utf-8 -*-
"""Crawler for PHF Science digital library records."""

from __future__ import annotations

import os
import json
import re
import subprocess
import time
from datetime import datetime
from html import unescape
from urllib.parse import urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class PhfscienceNzDigitalLibraryCrawler(BaseCrawler):
    site_id = "phfscience-nz-digital-library"
    site_name = "Custom: phfscience-nz-digital-library"
    base_url = "https://www.phfscience.nz"

    START_URL = "https://www.phfscience.nz/digital-library/"
    ALGOLIA_APP_ID = "EE0122B7GY"
    ALGOLIA_API_KEY = os.environ.get("PHFSCIENCE_NZ_DIGITAL_LIBRARY_KEY", "")
    ALGOLIA_INDEX = "prod_esr_latest"
    ALGOLIA_QUERY_URL = (
        "https://EE0122B7GY-dsn.algolia.net/1/indexes/prod_esr_latest/query"
    )

    PAGE_SIZE = 100
    SAFETY_PAGE_CAP = 200
    WALL_CLOCK_SECONDS = 25 * 60
    DEADLINE_MARGIN_SECONDS = 15
    CURL_TIMEOUT = 45
    BACKOFF_SECONDS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 50
    MIN_SAVED_ABSTRACT_CHARS = 100
    CURL_META_MARKER = "__PHFSCIENCE_CURL_META__:"

    DIGITAL_LIBRARY_TYPES = (
        "reportItem",
        "publishedResearchItem",
        "riskAssessmentItem",
        "conferencePosterItem",
        "isolatesSummaryItem",
        "dashboardItem",
    )

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl the public Algolia list API and HTML detail pages."""
        saved = 0
        global_page = 0
        seen_urls = set()
        started_at = time.monotonic()
        limit_or_inf = limit if limit is not None else "inf"

        years = self._fetch_year_partitions()
        if not years:
            years = [None]

        for year in years:
            if limit is not None and saved >= limit:
                break
            if self._time_nearly_up(started_at):
                print(f"[{self.site_id}] approaching 25 minute crawl budget; exiting cleanly")
                break

            page = 0
            while True:
                if limit is not None and saved >= limit:
                    break
                if self._time_nearly_up(started_at):
                    print(f"[{self.site_id}] approaching 25 minute crawl budget; exiting cleanly")
                    return saved
                if global_page >= self.SAFETY_PAGE_CAP:
                    print(f"[{self.site_id}] reached safety page cap of {self.SAFETY_PAGE_CAP}; stopping")
                    return saved

                data = self._fetch_algolia_page(page=page, year=year)
                if not data:
                    label = f"year {year}" if year else "unpartitioned query"
                    print(f"[{self.site_id}] list API failed for {label} page {page + 1}; stopping partition")
                    break

                hits = data.get("hits") or []
                nb_pages = data.get("nbPages")
                if not hits:
                    break

                global_page += 1
                if global_page % 10 == 0:
                    print(f"[{self.site_id}] page {global_page}: saved {saved}/{limit_or_inf}")

                records = self._parse_list_hits(hits)
                new_records = []
                for record in records:
                    detail_url = record.get("url") or ""
                    if not detail_url or detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_records.append(record)

                if not new_records:
                    print(f"[{self.site_id}] page {global_page} returned 0 new records; stopping partition")
                    break

                for idx, record in enumerate(new_records, start=1):
                    if limit is not None and saved >= limit:
                        break
                    if self._time_nearly_up(started_at):
                        print(f"[{self.site_id}] approaching 25 minute crawl budget; exiting cleanly")
                        return saved

                    item_label = f"{global_page}.{idx}"
                    try:
                        time.sleep(self.detail_delay)
                        detail_raw = self._curl_get(
                            record["url"],
                            context=f"item {item_label} detail",
                            referer=self.START_URL,
                        )
                        if not detail_raw:
                            raise RuntimeError("detail fetch failed after retries")

                        detail_soup = self._make_soup(
                            detail_raw,
                            context=f"item {item_label} detail",
                        )
                        if detail_soup is None:
                            raise RuntimeError("detail HTML could not be parsed")

                        parsed = self._parse_detail(detail_soup, record)
                        abstract = parsed.get("abstract") or ""
                        if len(abstract) < self.MIN_ABSTRACT_CHARS:
                            print(
                                f"[{self.site_id}] item {item_label} skipped: "
                                f"abstract shorter than 50 chars ({len(abstract)} chars)"
                            )
                            continue
                        if len(abstract) < self.MIN_SAVED_ABSTRACT_CHARS:
                            print(
                                f"[{self.site_id}] item {item_label} skipped: "
                                f"abstract shorter than {self.MIN_SAVED_ABSTRACT_CHARS} chars "
                                f"({len(abstract)} chars)"
                            )
                            continue

                        paper = {
                            "id": None,
                            "site_id": self.site_id,
                            "external_id": parsed["external_id"],
                            "title": parsed["title"],
                            "authors": json.dumps(parsed["authors"], ensure_ascii=False),
                            "abstract": abstract,
                            "category": parsed["category"],
                            "keywords": json.dumps(parsed["keywords"], ensure_ascii=False),
                            "published_date": parsed["published_date"],
                            "url": parsed["url"],
                            "pdf_url": parsed["pdf_url"],
                            "doi": parsed["doi"],
                            "department": parsed["department"],
                            "metadata": json.dumps(parsed["metadata"], ensure_ascii=False),
                        }
                        self._save_paper(paper)
                        saved += 1
                        counter = f"{saved}/{limit}" if limit is not None else str(saved)
                        print(f"[{self.site_id}] saved {counter}: {parsed['title'][:90]}")
                    except Exception as exc:
                        print(f"[{self.site_id}] item {item_label} failed: {exc}")
                        continue

                if limit is not None and saved >= limit:
                    break
                if nb_pages is not None and page + 1 >= int(nb_pages):
                    break
                page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Algolia list API
    # ------------------------------------------------------------------

    def _fetch_year_partitions(self):
        params = {
            "query": "",
            "hitsPerPage": 0,
            "page": 0,
            "facets": json.dumps(["year"], separators=(",", ":")),
            "facetFilters": self._facet_filters(),
        }
        data = self._algolia_query(params, context="year facet query")
        if not data:
            return []

        years = []
        facets = data.get("facets") or {}
        for raw_year in (facets.get("year") or {}).keys():
            if re.fullmatch(r"\d{4}", str(raw_year)):
                years.append(str(raw_year))
        years.sort(key=lambda value: int(value), reverse=True)
        return years

    def _fetch_algolia_page(self, page, year=None):
        params = {
            "query": "",
            "hitsPerPage": self.PAGE_SIZE,
            "page": page,
            "facetFilters": self._facet_filters(year=year),
        }
        return self._algolia_query(
            params,
            context=f"list API year {year or 'all'} page {page + 1}",
        )

    def _facet_filters(self, year=None):
        filters = [[f"contentTypeAlias:{alias}" for alias in self.DIGITAL_LIBRARY_TYPES]]
        if year:
            filters.append(f"year:{year}")
        return json.dumps(filters, separators=(",", ":"))

    def _algolia_query(self, params, context):
        body = json.dumps({"params": urlencode(params)}, separators=(",", ":"))
        headers = [
            "-H",
            f"X-Algolia-API-Key: {self.ALGOLIA_API_KEY}",
            "-H",
            f"X-Algolia-Application-Id: {self.ALGOLIA_APP_ID}",
            "-H",
            "Content-Type: application/json",
            "-H",
            "Accept: application/json",
            "-H",
            f"Referer: {self.START_URL}",
        ]
        raw = self._curl_post_json(
            self.ALGOLIA_QUERY_URL,
            body,
            headers=headers,
            context=context,
        )
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] {context} returned invalid JSON: {exc}")
            return None
        if data.get("message") and not data.get("hits"):
            print(f"[{self.site_id}] {context} API message: {data.get('message')}")
        return data

    def _parse_list_hits(self, hits):
        records = []
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            url = self._absolute_url(hit.get("url") or "")
            if not self._is_digital_library_url(url):
                continue

            title = self._clean_text(hit.get("title") or "")
            if not title:
                continue

            record = {
                "external_id": str(hit.get("id") or hit.get("objectID") or self._slug_from_url(url)),
                "object_id": hit.get("objectID") or "",
                "title": title,
                "abstract": self._clean_multiline(hit.get("description") or hit.get("metaDescription") or ""),
                "published_date": self._parse_date(hit.get("date") or ""),
                "url": url,
                "pdf_url": self._download_url(hit),
                "category": self._clean_text(hit.get("type") or hit.get("contentTypeAlias") or ""),
                "authors": self._parse_authors(hit.get("authors") or ""),
                "department": self._clean_text(hit.get("department") or ""),
                "source": self._clean_text(hit.get("source") or ""),
                "keywords": self._keywords_from_hit(hit),
                "raw_hit": self._metadata_hit(hit),
            }
            records.append(record)
        return records

    # ------------------------------------------------------------------
    # Detail parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, soup, record):
        canonical_url = (
            self._meta_content(soup, "og:url")
            or self._link_href(soup, "link[rel='canonical']")
            or record.get("url")
            or ""
        )
        url = self._absolute_url(canonical_url)

        title = (
            self._node_text(soup.select_one("main h3.heading-3"))
            or self._clean_text(self._meta_content(soup, "og:title", "title"))
            or record.get("title")
            or ""
        )
        if not title:
            raise RuntimeError("detail page has no title")

        detail_meta = self._detail_metadata(soup)
        summary = self._summary_text(soup)
        abstract = self._clean_multiline(summary or record.get("abstract") or "")

        published_date = (
            self._parse_date(detail_meta.get("date") or "")
            or record.get("published_date")
            or ""
        )
        category = detail_meta.get("type") or record.get("category") or ""
        pdf_url = self._detail_pdf_url(soup) or record.get("pdf_url") or ""
        keywords = list(record.get("keywords") or [])

        metadata = {
            "source": record.get("source") or "",
            "journal": record.get("source") or "",
            "algoliaObjectID": record.get("object_id") or "",
            "contentTypeAlias": (record.get("raw_hit") or {}).get("contentTypeAlias", ""),
            "detailMetadata": detail_meta,
            "listApiEndpoint": self.ALGOLIA_QUERY_URL,
            "detailEndpointType": "HTML detail page",
            "download": (record.get("raw_hit") or {}).get("download"),
            "image": (record.get("raw_hit") or {}).get("image"),
            "posted_date": (record.get("raw_hit") or {}).get("createDate", ""),
            "updated_date": (record.get("raw_hit") or {}).get("updateDate", ""),
            "originalFilename": self._filename_from_url(pdf_url),
        }

        return {
            "external_id": record.get("external_id") or self._slug_from_url(url),
            "title": title,
            "authors": record.get("authors") or [],
            "abstract": abstract,
            "category": category,
            "keywords": keywords,
            "published_date": published_date,
            "url": url,
            "pdf_url": pdf_url,
            "doi": self._extract_doi(record, soup),
            "department": record.get("department") or "PHF Science",
            "metadata": metadata,
        }

    def _detail_metadata(self, soup):
        metadata = {}
        aside = soup.select_one("main aside") or soup.select_one("aside")
        if aside is None:
            return metadata
        for block in aside.select("div"):
            label = self._node_text(block.select_one("h6"))
            value = self._node_text(block.select_one("p"))
            if not label or not value:
                continue
            key = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
            if key:
                metadata[key] = value
        return metadata

    def _summary_text(self, soup):
        for heading in soup.find_all(["h2", "h3", "h4", "h5", "h6"]):
            if self._node_text(heading).strip().lower() == "summary":
                parent = heading.find_parent("div")
                if parent:
                    paragraphs = [
                        self._node_text(p)
                        for p in parent.find_all(["p", "li"], recursive=True)
                    ]
                    text = self._clean_multiline("\n".join(p for p in paragraphs if p))
                    if text:
                        return text
                sibling = heading.find_next(["p", "div"])
                text = self._node_text(sibling)
                if text:
                    return text
        return ""

    def _detail_pdf_url(self, soup):
        for link in soup.select("a[href]"):
            href = (link.get("href") or "").strip()
            if not href:
                continue
            text = self._node_text(link).lower()
            download_name = link.get("download")
            if href.lower().split("?", 1)[0].endswith(".pdf") or download_name or "download" in text:
                return self._absolute_url(href)
        return ""

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, context="request", referer=None):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--connect-timeout",
            "15",
            "--max-time",
            str(self.CURL_TIMEOUT),
            "-A",
            self.USER_AGENT,
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "-H",
            "Accept-Language: en-NZ,en;q=0.9",
            "-w",
            "\n" + self.CURL_META_MARKER + "%{http_code}\t%{url_effective}",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)
        return self._run_curl(cmd, context=context)

    def _curl_post_json(self, url, body, headers=None, context="request"):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--connect-timeout",
            "15",
            "--max-time",
            str(self.CURL_TIMEOUT),
            "-A",
            self.USER_AGENT,
        ]
        if headers:
            cmd.extend(headers)
        cmd.extend(
            [
                "--data-raw",
                body,
                "-w",
                "\n" + self.CURL_META_MARKER + "%{http_code}\t%{url_effective}",
                url,
            ]
        )
        return self._run_curl(cmd, context=context)

    def _run_curl(self, cmd, context="request"):
        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.CURL_TIMEOUT + 10,
                )
                stdout = (result.stdout or b"").decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                body, http_code, effective_url = self._split_curl_output(stdout, "")

                if result.returncode != 0:
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if http_code and int(http_code) >= 400:
                    raise RuntimeError(f"HTTP {http_code} for {effective_url}")
                if not body.strip():
                    raise RuntimeError(f"empty response for {effective_url}")
                return body
            except Exception as exc:
                last_error = str(exc)
                print(
                    f"[{self.site_id}] {context} curl failed "
                    f"(attempt {attempt}/3): {last_error}"
                )
                if attempt < 3:
                    wait = self.BACKOFF_SECONDS[attempt - 1]
                    print(f"[{self.site_id}] retrying in {wait}s...")
                    time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None

    def _split_curl_output(self, raw, fallback_url):
        marker_pos = raw.rfind("\n" + self.CURL_META_MARKER)
        if marker_pos == -1:
            return raw, "", fallback_url
        body = raw[:marker_pos]
        meta = raw[marker_pos + 1 + len(self.CURL_META_MARKER):].strip()
        if "\t" not in meta:
            return body, "", fallback_url
        http_code, effective_url = meta.split("\t", 1)
        return body, http_code.strip(), effective_url.strip() or fallback_url

    # ------------------------------------------------------------------
    # Generic parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, raw, context="HTML"):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                last_exc = exc
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        print(f"[{self.site_id}] all BeautifulSoup parsers failed for {context}: {last_exc}")
        return None

    def _node_text(self, node):
        if node is None:
            return ""
        return self._clean_multiline(node.get_text(" ", strip=True))

    def _meta_content(self, soup, *names):
        for name in names:
            selectors = [
                f'meta[name="{name}"]',
                f'meta[property="{name}"]',
                f'meta[property="og:{name}"]',
                f'meta[name="twitter:{name}"]',
            ]
            for selector in selectors:
                node = soup.select_one(selector)
                if node and node.get("content"):
                    return self._clean_text(node.get("content"))
        return ""

    def _link_href(self, soup, selector):
        node = soup.select_one(selector)
        if node and node.get("href"):
            return node.get("href").strip()
        return ""

    def _clean_text(self, value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def _clean_multiline(self, value):
        text = self._clean_text(value)
        return re.sub(r"\s*\n\s*", "\n", text).strip()

    def _parse_date(self, raw):
        text = self._clean_text(raw)
        if not text:
            return ""
        for fmt in (
            "%m/%d/%Y %I:%M:%S %p",
            "%m/%d/%Y",
            "%d %B %Y",
            "%d %b %Y",
            "%Y-%m-%d",
        ):
            try:
                dt = datetime.strptime(text, fmt)
                if dt.year < 1900:
                    return ""
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
        match = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text)
        if match:
            return self._parse_date(match.group(0))
        return ""

    def _absolute_url(self, url):
        value = self._clean_text(url)
        if not value:
            return ""
        return urljoin(self.base_url, value)

    def _is_digital_library_url(self, url):
        parsed = urlparse(url)
        return parsed.netloc in ("www.phfscience.nz", "phfscience.nz") and parsed.path.startswith(
            "/digital-library/"
        )

    def _download_url(self, hit):
        download = hit.get("download")
        if isinstance(download, dict):
            return self._absolute_url(download.get("url") or "")
        return ""

    def _keywords_from_hit(self, hit):
        keywords = []
        seen = set()
        for key in ("metaKeywords", "type", "department", "contentTypeAlias"):
            value = self._clean_text(hit.get(key) or "")
            if value and value not in seen:
                seen.add(value)
                keywords.append(value)

        facets = hit.get("TopicFacet") or {}
        if isinstance(facets, dict):
            for level in ("lvl0", "lvl1", "lvl2"):
                values = facets.get(level) or []
                if not isinstance(values, list):
                    values = [values]
                for value in values:
                    cleaned = self._clean_text(value)
                    if cleaned and cleaned not in seen:
                        seen.add(cleaned)
                        keywords.append(cleaned)
        return keywords

    def _parse_authors(self, raw):
        text = self._clean_text(raw)
        if not text:
            return []
        if ";" in text:
            return [part.strip(" ,") for part in text.split(";") if part.strip(" ,")]
        return [text]

    def _extract_doi(self, record, soup):
        source = " ".join(
            [
                record.get("source") or "",
                record.get("abstract") or "",
                self._node_text(soup.select_one("main"))[:2000],
            ]
        )
        match = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", source)
        if not match:
            return ""
        return match.group(0).rstrip(".,;) ")

    def _slug_from_url(self, url):
        path = urlparse(url).path.rstrip("/")
        slug = path.rsplit("/", 1)[-1]
        return slug or url

    def _filename_from_url(self, url):
        if not url:
            return ""
        tail = urlparse(url).path.rsplit("/", 1)[-1]
        return tail if "." in tail else ""

    def _metadata_hit(self, hit):
        metadata = {}
        for key in (
            "id",
            "objectID",
            "createDate",
            "updateDate",
            "contentTypeAlias",
            "sortTimestamp",
            "year",
            "source",
            "type",
            "download",
            "TopicFacet",
            "image",
            "metaKeywords",
        ):
            if key in hit:
                metadata[key] = hit.get(key)
        return metadata

    def _time_nearly_up(self, started_at):
        elapsed = time.monotonic() - started_at
        return elapsed >= self.WALL_CLOCK_SECONDS - self.DEADLINE_MARGIN_SECONDS
