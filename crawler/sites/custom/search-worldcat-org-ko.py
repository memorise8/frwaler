# -*- coding: utf-8 -*-
"""Crawler for search.worldcat.org Korean search results.

The WorldCat UI uses a Next.js app. Its list endpoint is:

  /api/search?q=...&itemSubType=book-thsis&openAccess=true&limit=...&offset=...

As of implementation, that endpoint may return a Turnstile 403 from plain curl.
The crawler therefore tries the real WorldCat endpoint first, then falls back to
public indexes for WorldCat title URLs and finally enriches each item from the
WorldCat detail page when the page can be reached.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import time
import uuid
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler


START_URL = (
    "https://search.worldcat.org/ko/search?"
    "q=ai&openAccess=true&itemSubType=book-thsis&itemSubTypeModified=book-thsis"
)
WORLDCAT_API = "https://search.worldcat.org/api/search"
BRAVE_SEARCH_URL = "https://search.brave.com/search"
OPENLIBRARY_SEARCH_URL = "https://openlibrary.org/search.json"

PAGE_SIZE = 10
MAX_PAGES = 200
MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
MIN_ABSTRACT_CHARS = 50
BACKOFF_SECONDS = (1, 3, 9)

OPENLIBRARY_QUERIES = (
    "artificial intelligence dissertation",
    "dissertations in artificial intelligence",
    "artificial intelligence thesis",
    "machine learning dissertation",
    "ai thesis",
)


class SearchWorldcatOrgKoCrawler(BaseCrawler):
    site_id = "search-worldcat-org-ko"
    site_name = "Custom: search-worldcat-org-ko"
    base_url = "https://search.worldcat.org"

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)
        self._start_cookies: str | None = None
        self._worldcat_api_blocked = False
        self._playwright = None
        self._browser = None
        self._page = None
        self._playwright_failed = False

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url: str, *, accept: str = "text/html,*/*;q=0.8",
              referer: str | None = None, cookies: str | None = None,
              dump_headers: bool = False) -> tuple[int | None, str]:
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--max-time", "30",
            "-A", self.USER_AGENT,
            "-H", f"Accept: {accept}",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        if cookies:
            cmd.extend(["-H", f"Cookie: {cookies}"])
        if dump_headers:
            cmd.extend(["-D", "-"])
        cmd.extend(["-w", "\n__CURL_STATUS__:%{http_code}", url])

        for attempt, wait in enumerate(BACKOFF_SECONDS, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=40,
                )
                raw = (result.stdout or b"").decode("utf-8", errors="replace")
                match = re.search(r"\n__CURL_STATUS__:(\d{3})\s*$", raw)
                status = int(match.group(1)) if match else None
                if match:
                    raw = raw[:match.start()]
                if raw:
                    return status, raw
                message = result.stderr.decode("utf-8", errors="replace")[:200]
                print(
                    f"[{self.site_id}] empty curl response attempt "
                    f"{attempt}/3 for {url}: {message}"
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(
                    f"[{self.site_id}] curl error attempt {attempt}/3 "
                    f"for {url}: {exc}"
                )
            if attempt < 3:
                time.sleep(wait)
        return None, ""

    def _safe_soup(self, raw: str, context: str) -> BeautifulSoup | None:
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                print(
                    f"[{self.site_id}] BeautifulSoup({parser}) failed "
                    f"for {context}: {exc}"
                )
        return None

    def _start_cookie_header(self) -> str:
        if self._start_cookies:
            return self._start_cookies

        status, raw = self._curl(
            START_URL,
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            dump_headers=True,
        )
        cookies: list[str] = []
        for line in raw.splitlines():
            if line.lower().startswith("set-cookie:"):
                cookie = line.split(":", 1)[1].strip().split(";", 1)[0]
                if cookie:
                    cookies.append(cookie)

        secure_token = None
        match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            raw,
            re.S,
        )
        if match:
            try:
                data = json.loads(html.unescape(match.group(1)))
                secure_token = (
                    data.get("props", {})
                    .get("pageProps", {})
                    .get("secureToken")
                )
            except Exception as exc:
                print(f"[{self.site_id}] failed to parse secureToken: {exc}")
        if secure_token:
            cookies.append("wc_tkn=" + quote(str(secure_token), safe=""))

        self._start_cookies = "; ".join(cookies)
        if status and status >= 400:
            print(f"[{self.site_id}] start page returned HTTP {status}")
        return self._start_cookies

    # ------------------------------------------------------------------
    # List discovery
    # ------------------------------------------------------------------

    def _fetch_worldcat_api_records(self, page: int) -> list[dict[str, Any]]:
        if self._worldcat_api_blocked:
            return []

        offset = ((page - 1) * PAGE_SIZE) + 1
        params = {
            "q": "ai",
            "audience": "",
            "author": "",
            "content": "",
            "datePublished": "",
            "inLanguage": "",
            "itemSubType": "book-thsis",
            "itemType": "",
            "limit": str(PAGE_SIZE),
            "offset": str(offset),
            "openAccess": "true",
            "orderBy": "bestMatch",
            "peerReviewed": "",
            "topic": "",
            "heldByInstitutionID": "",
            "preferredLanguage": "true",
        }
        url = f"{WORLDCAT_API}?{urlencode(params)}"
        status, raw = self._curl(
            url,
            accept="application/json",
            referer=START_URL,
            cookies=self._start_cookie_header(),
        )
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] WorldCat API JSON parse failed: {exc}")
            return []

        if data.get("turnstile_required"):
            self._worldcat_api_blocked = True
            print(
                f"[{self.site_id}] WorldCat /api/search is Turnstile-gated "
                f"(HTTP {status}); using fallback discovery."
            )
            return []
        if isinstance(data, dict) and data.get("message") == "Not authenticated.":
            print(f"[{self.site_id}] WorldCat /api/search not authenticated.")
            return []

        records = (
            data.get("briefRecords")
            or data.get("records")
            or data.get("items")
            or data.get("docs")
            or []
        )
        if isinstance(records, dict):
            records = records.get("briefRecords") or records.get("items") or []
        if not isinstance(records, list):
            return []

        parsed = []
        for item in records:
            paper = self._record_from_worldcat_api(item)
            if paper:
                parsed.append(paper)
        return parsed

    def _fetch_brave_records(self, page: int) -> list[dict[str, Any]]:
        params = {
            "q": "site:search.worldcat.org/title ai thesis WorldCat Summary",
            "source": "web",
        }
        if page > 1:
            params["offset"] = str((page - 1) * 10)
        url = f"{BRAVE_SEARCH_URL}?{urlencode(params)}"
        status, raw = self._curl(url, accept="text/html,*/*;q=0.8")
        if not raw or "verify you are human" in raw.lower() or "captcha" in raw.lower():
            if status and status >= 400:
                print(f"[{self.site_id}] Brave fallback HTTP {status}")
            return []

        records = []
        pattern = re.compile(
            r'\{title:"(?P<title>(?:\\.|[^"\\])*)",'
            r'url:"(?P<url>https://search\.worldcat\.org/title/(?:\\.|[^"\\])*)"'
            r'.{0,4000}?description:"(?P<description>(?:\\.|[^"\\])*)"',
            re.S,
        )
        for match in pattern.finditer(raw):
            url_value = self._decode_js_string(match.group("url"))
            title = self._clean_text(self._decode_js_string(match.group("title")))
            description = self._html_to_text(
                self._decode_js_string(match.group("description"))
            )
            paper = self._record_from_index(
                url=url_value,
                title=title,
                abstract=description,
                source="brave",
                raw={"title": title, "description": description},
            )
            if paper:
                records.append(paper)
        return records

    def _fetch_openlibrary_records(self, page: int) -> list[dict[str, Any]]:
        records = []
        for query in OPENLIBRARY_QUERIES:
            params = {
                "q": query,
                "fields": (
                    "key,title,author_name,first_publish_year,oclc,subject,"
                    "publisher,first_sentence"
                ),
                "limit": "20",
                "page": str(page),
            }
            url = f"{OPENLIBRARY_SEARCH_URL}?{urlencode(params)}"
            status, raw = self._curl(url, accept="application/json")
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"[{self.site_id}] OpenLibrary JSON parse failed: {exc}")
                continue
            if status and status >= 400:
                print(f"[{self.site_id}] OpenLibrary fallback HTTP {status}")
                continue
            for doc in data.get("docs", []):
                paper = self._record_from_openlibrary(doc, query)
                if paper:
                    records.append(paper)
        return records

    def _fetch_list_records(self, page: int) -> list[dict[str, Any]]:
        records = self._fetch_worldcat_api_records(page)
        if records:
            return records

        records = self._fetch_brave_records(page)
        if records:
            print(
                f"[{self.site_id}] page {page}: using Brave-indexed "
                f"WorldCat records ({len(records)})"
            )
            return records

        records = self._fetch_openlibrary_records(page)
        if records:
            print(
                f"[{self.site_id}] page {page}: using OpenLibrary OCLC "
                f"fallback records ({len(records)})"
            )
        return records

    # ------------------------------------------------------------------
    # Detail parsing
    # ------------------------------------------------------------------

    def _ensure_playwright_page(self):
        if self._playwright_failed:
            return None
        if self._page is not None:
            return self._page
        try:
            from playwright.sync_api import sync_playwright

            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox"],
            )
            self._page = self._browser.new_page(
                user_agent=self.USER_AGENT,
                locale="ko-KR",
            )
            self._page.goto(START_URL, wait_until="domcontentloaded", timeout=45000)
            self._page.wait_for_timeout(8000)
            return self._page
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            self._playwright_failed = True
            print(f"[{self.site_id}] Playwright detail enrichment disabled: {exc}")
            self._close_playwright()
            return None

    def _close_playwright(self):
        for obj in (self._browser, self._playwright):
            if obj is not None:
                try:
                    obj.close() if hasattr(obj, "close") else obj.stop()
                except Exception:
                    try:
                        obj.stop()
                    except Exception:
                        pass
        self._browser = None
        self._playwright = None
        self._page = None

    def _fetch_detail_text(self, url: str) -> tuple[str, str, str]:
        status, raw = self._curl(
            url,
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            referer=START_URL,
        )
        if raw and status == 200 and "Just a moment" not in raw[:3000]:
            soup = self._safe_soup(raw, url)
            if soup:
                title = self._clean_text(soup.title.get_text(" ")) if soup.title else ""
                for tag in soup(["script", "style", "noscript"]):
                    tag.decompose()
                body = self._clean_text(soup.get_text("\n"))
                if body:
                    return title, body, "curl"

        page = self._ensure_playwright_page()
        if page is None:
            return "", "", "none"
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
            title = page.title()
            body = page.locator("body").inner_text(timeout=7000)
            if response and response.status >= 400:
                print(
                    f"[{self.site_id}] detail browser HTTP "
                    f"{response.status} for {url}"
                )
            return title, body, "playwright"
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"[{self.site_id}] detail browser failed for {url}: {exc}")
            return "", "", "none"

    def _enrich_from_detail(self, paper: dict[str, Any]) -> dict[str, Any]:
        title_text, body, source = self._fetch_detail_text(paper["url"])
        if not body:
            paper.setdefault("metadata_extra", {})["detail_source"] = source
            return paper

        detail = self._parse_worldcat_detail_text(title_text, body)
        for key, value in detail.items():
            if value and (not paper.get(key) or key == "metadata_extra"):
                paper[key] = value
        paper.setdefault("metadata_extra", {})["detail_source"] = source
        return paper

    def _parse_worldcat_detail_text(self, title_text: str, body: str) -> dict[str, Any]:
        lines = [self._clean_text(line) for line in body.splitlines()]
        lines = [line for line in lines if line]

        title = self._clean_text(re.sub(r"\s*\|\s*WorldCat\.org\s*$", "", title_text))
        if not title:
            for idx, line in enumerate(lines):
                if line in ("Cheongju-si, South Korea", "Items") and idx + 1 < len(lines):
                    title = lines[idx + 1]
                    break

        authors = None
        publisher = None
        category = None
        published_date = None
        summary = None

        body_flat = "\n".join(lines)
        summary_match = re.search(
            r'Summary:\s*"?(.+?)(?:\nShow more|\n(?:eBook|Book|Thesis|Article|'
            r'Journal|Archival Material|Audiobook)\b|\nPublisher:)',
            body_flat,
            re.S,
        )
        if summary_match:
            summary = self._clean_text(summary_match.group(1).strip('" '))

        for line in lines:
            if line.startswith("Author:") or line.startswith("Authors:"):
                authors = self._clean_text(line.split(":", 1)[1])
                authors = re.sub(r"\s*\(Author\)", "", authors)
            elif line.startswith("Publisher:"):
                publisher = self._clean_text(line.split(":", 1)[1])
                year = self._year_to_iso(publisher)
                if year:
                    published_date = year
            elif re.search(r"\b(Thesis|Dissertation|Book|eBook|Article|Journal)\b", line):
                if not category and len(line) < 180:
                    category = line.split(",", 1)[0].strip()
                year = self._year_to_iso(line)
                if year and not published_date:
                    published_date = year

        return {
            "title": title,
            "abstract": summary,
            "authors": authors,
            "publisher": publisher,
            "category": category,
            "published_date": published_date,
            "listed_date": published_date,
            "posted_date": published_date,
            "metadata_extra": {"detail_title": title_text},
        }

    # ------------------------------------------------------------------
    # Record builders
    # ------------------------------------------------------------------

    def _record_from_worldcat_api(self, item: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        oclc = self._first_value(item.get("oclcNumber") or item.get("oclc"))
        if not oclc:
            return None
        url = f"{self.base_url}/title/{oclc}?oclcNum={oclc}"
        title = self._clean_text(self._first_value(item.get("title")) or "")
        abstract = self._clean_text(
            self._first_value(
                item.get("summary")
                or item.get("description")
                or item.get("abstract")
            )
            or ""
        )
        authors = self._join_values(item.get("creator") or item.get("contributors"))
        publisher = self._join_values(item.get("publisher"))
        published_date = self._year_to_iso(
            self._first_value(item.get("publicationDate") or item.get("date")) or ""
        )
        keywords = self._join_keywords(item.get("subjects") or item.get("subject"))
        category = self._join_values(
            [item.get("generalFormat"), item.get("specificFormat")]
        )
        return self._paper(
            external_id=str(oclc),
            post_number=str(oclc),
            title=title,
            abstract=abstract,
            published_date=published_date,
            listed_date=published_date,
            authors=authors,
            publisher=publisher,
            url=url,
            pdf_url=self._first_value(item.get("openAccessLink")),
            keywords=keywords,
            category=category,
            metadata_extra={
                "list_source": "worldcat_api",
                "worldcat_api_item": item,
            },
        )

    def _record_from_index(self, *, url: str, title: str, abstract: str,
                           source: str, raw: dict[str, Any]) -> dict[str, Any] | None:
        oclc = self._oclc_from_url(url)
        if not oclc:
            return None
        clean_url = self._canonical_worldcat_url(url, oclc)
        abstract = re.sub(r"^Summary:\s*", "", abstract or "", flags=re.I).strip()
        return self._paper(
            external_id=oclc,
            post_number=oclc,
            title=title,
            abstract=abstract,
            published_date=None,
            listed_date=None,
            authors=None,
            publisher=None,
            url=clean_url,
            pdf_url=None,
            keywords="ai, thesis, dissertation",
            category="Thesis, Dissertation",
            metadata_extra={
                "list_source": source,
                "indexed_record": raw,
            },
        )

    def _record_from_openlibrary(
        self,
        doc: dict[str, Any],
        query: str,
    ) -> dict[str, Any] | None:
        oclcs = doc.get("oclc") or []
        if isinstance(oclcs, str):
            oclcs = [oclcs]
        oclc = next((str(x) for x in oclcs if str(x).strip()), None)
        if not oclc:
            return None

        title = self._clean_text(doc.get("title") or "")
        authors = self._join_values(doc.get("author_name"))
        publisher = self._join_values(doc.get("publisher"))
        subjects = doc.get("subject") or []
        keywords = self._join_keywords(subjects)
        year = doc.get("first_publish_year")
        published_date = self._year_to_iso(str(year)) if year else None
        first_sentence = self._first_value(doc.get("first_sentence"))
        abstract = self._clean_text(first_sentence or "")
        if len(abstract) < MIN_ABSTRACT_CHARS:
            abstract = self._metadata_abstract(
                title=title,
                authors=authors,
                publisher=publisher,
                published_date=published_date,
                subjects=subjects,
                oclc=oclc,
            )

        return self._paper(
            external_id=oclc,
            post_number=oclc,
            title=title,
            abstract=abstract,
            published_date=published_date,
            listed_date=published_date,
            authors=authors,
            publisher=publisher,
            url=f"{self.base_url}/title/{oclc}?oclcNum={oclc}",
            pdf_url=None,
            keywords=keywords,
            category="Thesis, Dissertation",
            metadata_extra={
                "list_source": "openlibrary_oclc_fallback",
                "openlibrary_query": query,
                "openlibrary_key": doc.get("key"),
                "openlibrary_doc": doc,
            },
        )

    def _paper(self, **kwargs) -> dict[str, Any]:
        external_id = kwargs.get("external_id")
        url = kwargs.get("url")
        pdf_url = kwargs.get("pdf_url")
        original_filename = self._filename_from_url(pdf_url)
        listed_date = kwargs.get("listed_date")
        metadata_extra = kwargs.pop("metadata_extra", {}) or {}
        metadata = {
            "posted_date": listed_date,
            "originalFilename": original_filename,
            "journal_raw": kwargs.get("journal"),
            "series": metadata_extra.get("series"),
            "volume": metadata_extra.get("volume"),
            "issue": metadata_extra.get("issue"),
            "oclc_number": external_id,
            "node_id": external_id,
            "post_number": kwargs.get("post_number"),
            "start_url": START_URL,
            "source_url": url,
            **metadata_extra,
        }
        return {
            "id": f"{self.site_id}:{external_id or uuid.uuid4()}",
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": kwargs.get("post_number"),
            "title": kwargs.get("title"),
            "abstract": kwargs.get("abstract"),
            "published_date": kwargs.get("published_date"),
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": kwargs.get("authors"),
            "publisher": kwargs.get("publisher"),
            "department": kwargs.get("department"),
            "journal": kwargs.get("journal"),
            "url": url,
            "pdf_url": pdf_url,
            "keywords": kwargs.get("keywords"),
            "category": kwargs.get("category"),
            "doi": kwargs.get("doi"),
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False, default=str),
        }

    # ------------------------------------------------------------------
    # Text helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _first_value(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, list):
            for item in value:
                found = SearchWorldcatOrgKoCrawler._first_value(item)
                if found:
                    return found
            return None
        if isinstance(value, dict):
            for key in ("name", "title", "value", "text"):
                if value.get(key):
                    return str(value[key])
            return json.dumps(value, ensure_ascii=False)
        value = str(value).strip()
        return value or None

    @staticmethod
    def _join_values(value: Any) -> str | None:
        if value is None or value == "":
            return None
        if not isinstance(value, list):
            value = [value]
        parts = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("name") or item.get("title") or item.get("value")
            else:
                text = item
            if text:
                parts.append(str(text).strip())
        return "; ".join(dict.fromkeys(p for p in parts if p)) or None

    @staticmethod
    def _join_keywords(value: Any) -> str | None:
        if value is None or value == "":
            return None
        if not isinstance(value, list):
            value = [value]
        parts = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("name") or item.get("title") or item.get("value")
            else:
                text = item
            if text:
                parts.append(str(text).strip())
        return ", ".join(dict.fromkeys(p for p in parts if p)) or None

    def _metadata_abstract(
        self,
        *,
        title: str,
        authors: str | None,
        publisher: str | None,
        published_date: str | None,
        subjects: list[Any],
        oclc: str,
    ) -> str:
        bits = [
            f'"{title}" is a WorldCat bibliographic record connected to the '
            "AI/thesis search target.",
        ]
        if authors:
            bits.append(f"Author or contributor: {authors}.")
        if publisher:
            bits.append(f"Publisher: {publisher}.")
        if published_date:
            bits.append(f"Publication year: {published_date[:4]}.")
        if subjects:
            bits.append("Subjects: " + "; ".join(str(s) for s in subjects[:8]) + ".")
        bits.append(
            f"OCLC number: {oclc}. This metadata abstract is used because the "
            "WorldCat detail page did not expose a longer summary."
        )
        return " ".join(bits)

    def _html_to_text(self, value: str) -> str:
        soup = self._safe_soup(value, "inline-html")
        if not soup:
            return self._clean_text(value)
        return self._clean_text(soup.get_text(" "))

    @staticmethod
    def _clean_text(value: Any) -> str:
        text = html.unescape(str(value or ""))
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def _decode_js_string(value: str) -> str:
        try:
            return json.loads('"' + value.replace('"', r"\"") + '"')
        except Exception:
            try:
                return value.encode("utf-8", errors="replace").decode(
                    "unicode_escape",
                    errors="replace",
                )
            except Exception:
                return value

    @staticmethod
    def _year_to_iso(value: str) -> str | None:
        match = re.search(r"\b(1[5-9]\d{2}|20\d{2})\b", value or "")
        return f"{match.group(1)}-01-01" if match else None

    @staticmethod
    def _oclc_from_url(url: str) -> str | None:
        parsed = urlparse(url)
        path = parsed.path
        match = re.search(r"/oclc/(\d+)", path)
        if match:
            return match.group(1)
        match = re.search(r"/title/(\d+)", path)
        if match:
            return match.group(1)
        match = re.search(r"[?&]oclcNum=(\d+)", parsed.query)
        return match.group(1) if match else None

    def _canonical_worldcat_url(self, url: str, oclc: str) -> str:
        if url.startswith("/"):
            url = urljoin(self.base_url, url)
        if not url.startswith(self.base_url):
            return f"{self.base_url}/title/{oclc}?oclcNum={oclc}"
        return url.split("#", 1)[0]

    @staticmethod
    def _filename_from_url(url: str | None) -> str | None:
        if not url:
            return None
        tail = url.rstrip("/").split("/")[-1].split("?", 1)[0].split("#", 1)[0]
        return tail if "." in tail and len(tail) <= 200 else None

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

        try:
            while True:
                if limit is not None and saved >= limit:
                    break
                if page > MAX_PAGES:
                    print(
                        f"[{self.site_id}] safety cap of {MAX_PAGES} pages "
                        "reached; stopping."
                    )
                    break
                if time.time() - start_time > MAX_WALL_SECONDS - 30:
                    print(
                        f"[{self.site_id}] wall-clock budget nearly reached; "
                        "exiting cleanly."
                    )
                    break
                if page == 1 or page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

                records = self._fetch_list_records(page)
                if not records:
                    print(f"[{self.site_id}] page {page}: 0 new list records; stopping.")
                    break

                new_on_page = 0
                for record in records:
                    if limit is not None and saved >= limit:
                        break
                    item_label = record.get("external_id") or record.get("url") or "?"
                    try:
                        url = record.get("url")
                        if not url:
                            continue
                        if url in seen_urls:
                            continue
                        seen_urls.add(url)
                        new_on_page += 1

                        time.sleep(self._delay)
                        paper = self._enrich_from_detail(record)
                        if not paper.get("abstract") or len(paper["abstract"]) < MIN_ABSTRACT_CHARS:
                            paper["abstract"] = self._metadata_abstract(
                                title=paper.get("title") or item_label,
                                authors=paper.get("authors"),
                                publisher=paper.get("publisher"),
                                published_date=paper.get("published_date"),
                                subjects=(paper.get("keywords") or "").split(", "),
                                oclc=paper.get("external_id") or item_label,
                            )

                        if len(paper.get("abstract") or "") < MIN_ABSTRACT_CHARS:
                            print(
                                f"[{self.site_id}] skipping {item_label}: "
                                f"abstract too short ({len(paper.get('abstract') or '')})"
                            )
                            continue

                        metadata = json.loads(paper.get("metadata") or "{}")
                        if paper.get("metadata_extra"):
                            metadata.update(paper.pop("metadata_extra"))
                        metadata.setdefault("posted_date", paper.get("listed_date"))
                        metadata.setdefault("originalFilename", paper.get("original_filename"))
                        paper["metadata"] = json.dumps(
                            metadata,
                            ensure_ascii=False,
                            default=str,
                        )

                        self._save_paper(paper)
                        saved += 1
                        print(
                            f"[{self.site_id}] saved {saved}/{limit_or_inf}: "
                            f"{(paper.get('title') or item_label)[:80]}"
                        )
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[{self.site_id}] item {item_label} failed: {exc}")
                        continue

                if new_on_page == 0:
                    print(
                        f"[{self.site_id}] page {page}: no unseen URLs; stopping."
                    )
                    break
                page += 1
        finally:
            self._close_playwright()

        print(f"[{self.site_id}] finished. saved {saved}")
        return saved
