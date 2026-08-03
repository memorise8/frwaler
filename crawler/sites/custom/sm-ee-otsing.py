# -*- coding: utf-8 -*-
"""Crawler for sm.ee (Sotsiaalministeerium / Estonian Ministry of Social
Affairs) `/otsing` search, filtered to ``Artikkel`` (article) content.

The `/otsing` page itself is a static shell — the Vue search widget calls a
separate VPortal search microservice
(``search.service.eu-live.vportal.ee/v1/search/sm``) with
``filters[type]``, ``sort_by``, ``page``, ``limit``, ``langcode`` query
params and returns Solr-style JSON (``response.docs``). The service
requires a ``Referer``/``Origin`` header matching the site or it returns a
bare ``null`` body. Detail pages are plain Drupal node pages fetched at
``base_url + doc["uri"]``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


_SITE_ID = "sm-ee-otsing"


class SmEeOtsingCrawler(BaseCrawler):
    site_id = "sm-ee-otsing"
    site_name = "Custom: sm-ee-otsing"
    base_url = "https://www.sm.ee"

    START_URL = "https://www.sm.ee/otsing?filters%5Btype%5D=Artikkel&sort=created&page=1"
    SEARCH_API = "https://search.service.eu-live.vportal.ee/v1/search/sm"
    SEARCH_REFERER = "https://www.sm.ee/otsing"
    FILTER_TYPE = "Artikkel"

    PAGE_SIZE = 20
    MAX_PAGES = 200
    WALL_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    CURL_TIMEOUT = 30
    BACKOFF_SECONDS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 100
    PUBLISHER = "Sotsiaalministeerium"
    CURL_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        start_time = time.monotonic()
        limit_or_inf = str(limit) if limit is not None else "inf"

        print(f"[{_SITE_ID}] starting crawl (limit={limit_or_inf})")
        print(f"[{_SITE_ID}] list endpoint: {self.SEARCH_API} (filters[type]={self.FILTER_TYPE})")
        print(f"[{_SITE_ID}] detail endpoint: node HTML page (base_url + doc['uri'])")

        for page in range(1, self.MAX_PAGES + 1):
            if limit is not None and saved >= limit:
                break

            elapsed = time.monotonic() - start_time
            if elapsed > self.WALL_BUDGET_SECONDS:
                print(f"[{_SITE_ID}] 25-minute budget reached at page {page}; stopping")
                break

            if page % 10 == 0 or page == 1:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_or_inf}")

            data = self._fetch_search_page(page)
            if data is None:
                print(f"[{_SITE_ID}] page {page}: search API failed; stopping")
                break

            docs = ((data.get("response") or {}).get("docs")) or []
            if not docs:
                print(f"[{_SITE_ID}] page {page}: 0 records; pagination complete")
                break

            page_had_new = False
            time_budget_reached = False
            for item_index, doc in enumerate(docs, start=1):
                if limit is not None and saved >= limit:
                    break
                elapsed = time.monotonic() - start_time
                if elapsed > self.WALL_BUDGET_SECONDS:
                    print(f"[{_SITE_ID}] 25-minute budget reached during page {page}; stopping")
                    time_budget_reached = True
                    break

                try:
                    record = self._parse_doc(doc)
                    if record is None:
                        continue

                    item_url = record["url"]
                    if item_url in seen_urls:
                        continue
                    seen_urls.add(item_url)
                    page_had_new = True

                    time.sleep(self.detail_delay)
                    detail_html = self._fetch_detail(item_url)
                    if detail_html is not None and self._is_bot_challenge(detail_html):
                        print(
                            f"[{_SITE_ID}] item {item_index} (page {page}): detail page "
                            f"blocked by bot challenge; keeping list-derived data only"
                        )
                        detail_html = None
                    if detail_html is not None:
                        self._enrich_from_detail(record, detail_html, item_url)

                    abstract = record.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{_SITE_ID}] item {item_index} (page {page}) skipped: "
                            f"abstract too short ({len(abstract)} chars): "
                            f"{(record.get('title') or '')[:60]}"
                        )
                        continue

                    record["metadata"] = json.dumps(record["metadata"], ensure_ascii=False)
                    self._save_paper(record)
                    saved += 1
                    print(f"[{_SITE_ID}] saved {saved}/{limit_or_inf}: {record['title'][:80]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    label = doc.get("title") or doc.get("uri") or f"item {item_index}"
                    print(f"[{_SITE_ID}] item {str(label)[:80]} failed: {exc}")
                    continue

            if time_budget_reached:
                break
            if not page_had_new:
                print(f"[{_SITE_ID}] page {page}: all URLs already seen; stopping")
                break
            if page == self.MAX_PAGES:
                print(f"[{_SITE_ID}] safety cap of {self.MAX_PAGES} pages reached; stopping")

        print(f"[{_SITE_ID}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Search API (list)
    # ------------------------------------------------------------------

    def _fetch_search_page(self, page):
        query = (
            f"filters%5Btype%5D={self.FILTER_TYPE}"
            f"&sort_by=created&page={page}&limit={self.PAGE_SIZE}&langcode=et"
        )
        url = f"{self.SEARCH_API}?{query}"
        body = self._curl_get(url, accept="application/json", context=f"search page {page}")
        if not body:
            return None
        try:
            data = json.loads(body)
        except Exception as exc:
            print(f"[{_SITE_ID}] search page {page} JSON parse failed: {exc}")
            return None
        if not isinstance(data, dict):
            return None
        return data

    # ------------------------------------------------------------------
    # Detail page fetch
    # ------------------------------------------------------------------

    def _fetch_detail(self, url):
        return self._curl_get(url, accept="text/html,application/xhtml+xml", context=f"detail {url}")

    def _curl_get(self, url, *, accept, context):
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
            "-H",
            f"User-Agent: {self.CURL_USER_AGENT}",
            "-H",
            f"Accept: {accept}",
            "-H",
            "Accept-Language: et,en;q=0.9",
            "-H",
            f"Referer: {self.SEARCH_REFERER}",
            "-H",
            "Origin: https://www.sm.ee",
            url,
        ]

        last_error = "unknown error"
        for attempt, wait in enumerate(self.BACKOFF_SECONDS, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.CURL_TIMEOUT + 10,
                    check=False,
                )
                text = (result.stdout or b"").decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                if result.returncode == 0 and text.strip():
                    return text
                last_error = f"curl exit={result.returncode} stderr={stderr[:200]}"
            except subprocess.TimeoutExpired as exc:
                last_error = f"timeout after {exc.timeout}s"
            except Exception as exc:
                last_error = str(exc)

            if attempt < len(self.BACKOFF_SECONDS):
                print(
                    f"[{_SITE_ID}] {context} failed "
                    f"(attempt {attempt}/3): {last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{_SITE_ID}] {context} failed after 3 attempts: {last_error}")
        return None

    # ------------------------------------------------------------------
    # Search-hit -> paper record
    # ------------------------------------------------------------------

    def _parse_doc(self, doc):
        uri = doc.get("uri") or ""
        if not uri:
            return None
        url = urljoin(self.base_url, uri)

        title = self._clean(doc.get("title") or "")
        if not title:
            return None

        raw_id = doc.get("id") or doc.get("ss_search_api_id") or ""
        match = re.search(r"node/(\d+)", raw_id)
        node_id = match.group(1) if match else None
        external_id = node_id or uri.strip("/") or url

        created_raw = doc.get("created")
        listed_date = self._iso_date(created_raw)

        abstract = self._clean(doc.get("lead_text") or "")
        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            abstract = self._augment_abstract(abstract, doc, title)

        category = doc.get("ss_entity_bundle") or self.FILTER_TYPE

        metadata = {
            "posted_date": created_raw,
            "node_id": node_id,
            "ss_search_api_id": doc.get("ss_search_api_id"),
            "content_type": doc.get("content_type"),
            "langcode": doc.get("langcode"),
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "originalFilename": None,
        }

        return {
            "id": f"{self.site_id}:{external_id}",
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": node_id,
            "title": title,
            "abstract": abstract,
            "published_date": listed_date,
            "listed_date": listed_date,
            "authors": None,
            "publisher": self.PUBLISHER,
            "department": None,
            "journal": None,
            "url": url,
            "pdf_url": None,
            "keywords": None,
            "category": category,
            "doi": None,
            "original_filename": None,
            "metadata": metadata,
        }

    def _augment_abstract(self, abstract, doc, title):
        """Extend a short ``lead_text`` with real body fragments from ``content``."""
        parts = [abstract] if abstract else []
        seen = {abstract} if abstract else set()
        for raw in doc.get("content") or []:
            text = self._clean(raw)
            if not text or text == title or text in seen or len(text) < 15:
                continue
            parts.append(text)
            seen.add(text)
            if sum(len(p) for p in parts) >= self.MIN_ABSTRACT_CHARS:
                break
        return " ".join(parts).strip()

    # ------------------------------------------------------------------
    # Detail-page enrichment (keywords, pdf attachment, abstract fallback)
    # ------------------------------------------------------------------

    def _enrich_from_detail(self, record, html, item_url):
        soup = self._make_soup(html, context=f"detail {item_url}")
        if soup is None:
            return

        try:
            keyword_links = soup.select("div.field--name-field-keywords a")
            keywords = [self._clean(a.get_text()) for a in keyword_links]
            keywords = [k for k in keywords if k]
            if keywords:
                record["keywords"] = ", ".join(keywords)
        except Exception as exc:
            print(f"[{_SITE_ID}] {item_url}: keyword parse failed: {exc}")

        try:
            for a in soup.find_all("a", attrs={"download": True}):
                href = a.get("href") or ""
                if not href:
                    continue
                pdf_url = urljoin(self.base_url, href)
                text = self._clean(a.get_text(" "))
                original_filename = None
                if "|" in text:
                    original_filename = text.split("|", 1)[0].strip()
                original_filename = original_filename or self._filename_from_url(pdf_url)
                record["pdf_url"] = pdf_url
                record["original_filename"] = original_filename
                record["metadata"]["originalFilename"] = original_filename
                break
        except Exception as exc:
            print(f"[{_SITE_ID}] {item_url}: pdf link parse failed: {exc}")

        try:
            page_text = soup.get_text(" ")
            updated_match = re.search(
                r"Viimati uuendatud\s+(\d{1,2})\.(\d{1,2})\.(\d{4})", page_text
            )
            if updated_match:
                day, month, year = updated_match.groups()
                record["metadata"]["last_updated"] = f"{year}-{int(month):02d}-{int(day):02d}"
        except Exception as exc:
            print(f"[{_SITE_ID}] {item_url}: last-updated parse failed: {exc}")

        if len(record.get("abstract") or "") < self.MIN_ABSTRACT_CHARS:
            try:
                og_desc = soup.find("meta", attrs={"property": "og:description"})
                if og_desc and og_desc.get("content"):
                    candidate = self._clean(og_desc["content"])
                    if len(candidate) > len(record.get("abstract") or ""):
                        record["abstract"] = candidate
            except Exception as exc:
                print(f"[{_SITE_ID}] {item_url}: og:description parse failed: {exc}")

        if len(record.get("abstract") or "") < self.MIN_ABSTRACT_CHARS:
            try:
                body_paras = [self._clean(p.get_text(" ")) for p in soup.select("article p")]
                extra = " ".join(t for t in body_paras if len(t) >= 15)
                combined = ((record.get("abstract") or "") + " " + extra).strip()
                if len(combined) > len(record.get("abstract") or ""):
                    record["abstract"] = combined
            except Exception as exc:
                print(f"[{_SITE_ID}] {item_url}: body paragraph fallback failed: {exc}")

    # ------------------------------------------------------------------
    # Misc parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_soup(raw, *, context):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[{_SITE_ID}] BeautifulSoup({parser}) failed for {context}: {exc}")
        return None

    @staticmethod
    def _clean(value):
        if value is None:
            return ""
        return re.sub(r"\s+", " ", str(value)).strip()

    @staticmethod
    def _iso_date(value):
        if not value:
            return None
        match = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(value))
        return match.group(0) if match else None

    @staticmethod
    def _is_bot_challenge(html):
        if not html:
            return False
        head = html[:2000]
        markers = (
            "Just a moment",
            "cdn-cgi/challenge-platform",
            "Enable JavaScript and cookies to continue",
        )
        return any(marker in head for marker in markers)

    @staticmethod
    def _filename_from_url(url):
        if not url:
            return None
        path = urlparse(url).path
        tail = unquote(path.rstrip("/").split("/")[-1])
        if "." in tail and len(tail) <= 240:
            return tail
        return None
