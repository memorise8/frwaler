# -*- coding: utf-8 -*-
"""Crawler for NINDS (NIH) press releases.

The site exposes a Drupal Views JSON feed at
``/news-events/press-releases/press-releases.json`` that returns the full
press-release index (title HTML, ``field_release_date`` and ``body`` HTML).
The same feed also exists as RSS/CSV/XLSX and as the human-facing
``/news-events/news/press-releases`` Views page; the JSON feed is the
fastest fully-served representation. We still walk pages defensively in
case the feed ever starts paginating in the future, deduplicating by
detail URL so a feed that silently loops back to page 0 cannot drive an
infinite loop.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import unicodedata
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class NindsNihGovNewsEventsCrawler(BaseCrawler):
    site_id = "ninds-nih-gov-news-events"
    site_name = "Custom: ninds-nih-gov-news-events"
    base_url = "https://www.ninds.nih.gov"

    START_URL = "https://www.ninds.nih.gov/news-events/news/press-releases"
    LIST_ENDPOINT = (
        "https://www.ninds.nih.gov/news-events/press-releases/"
        "press-releases.json?page={page}&_format=json"
    )

    BACKOFF_SECONDS = (1, 3, 9)
    CURL_TIMEOUT = 60
    MIN_ABSTRACT_CHARS = 50
    MAX_ABSTRACT_CHARS = 8000
    PAGE_SAFETY_CAP = 200
    WALL_CLOCK_BUDGET_SECONDS = 25 * 60
    PROGRESS_EVERY = 10

    _CURL_META_MARKER = "__NINDS_CURL_META__:"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        # Detail processing is purely local (parsing the body field of the
        # JSON feed) so detail_delay can be very small. Default mirrors the
        # convention from sibling crawlers.
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 0
        seen_urls = set()
        start_time = time.monotonic()
        limit_label = str(limit) if limit is not None else "inf"

        while True:
            if limit is not None and saved >= limit:
                break
            if page >= self.PAGE_SAFETY_CAP:
                print(
                    f"[{self.site_id}] safety cap reached at page {page}; "
                    "stopping (defensive guard, not expected on this feed)"
                )
                break
            elapsed = time.monotonic() - start_time
            if elapsed > self.WALL_CLOCK_BUDGET_SECONDS:
                print(
                    f"[{self.site_id}] wall-clock budget exceeded "
                    f"({elapsed:.0f}s > {self.WALL_CLOCK_BUDGET_SECONDS}s); "
                    "exiting cleanly"
                )
                break

            list_url = self.LIST_ENDPOINT.format(page=page)
            raw, _effective_url = self._curl_get_text(
                list_url,
                context=f"list page {page}",
                referer=self.START_URL,
                accept="application/json,*/*;q=0.8",
            )
            if raw is None:
                print(
                    f"[{self.site_id}] list endpoint failed at page {page}; "
                    "stopping"
                )
                break

            records = self._parse_list_json(raw, context=f"page {page}")
            if records is None:
                # Hard-parse failure -> bail; partial saves already persisted.
                print(
                    f"[{self.site_id}] list page {page} could not be parsed; "
                    "stopping"
                )
                break

            new_in_page = 0
            for idx, record in enumerate(records, start=1):
                if limit is not None and saved >= limit:
                    break

                item_label = f"page {page} item {idx}"
                try:
                    detail_url = record.get("url") or ""
                    if not detail_url:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            "no detail URL"
                        )
                        continue
                    if detail_url in seen_urls:
                        # Drupal Views feeds can echo the full set on every
                        # page; treat already-seen URLs as paginator loops.
                        continue
                    seen_urls.add(detail_url)
                    new_in_page += 1

                    if self.detail_delay:
                        time.sleep(self.detail_delay)

                    parsed = self._parse_detail(record)
                    abstract = parsed.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": parsed["external_id"],
                        "title": parsed["title"],
                        "authors": json.dumps(
                            parsed["authors"], ensure_ascii=False
                        ),
                        "abstract": abstract,
                        "category": parsed["category"],
                        "keywords": json.dumps(
                            parsed["keywords"], ensure_ascii=False
                        ),
                        "published_date": parsed["published_date"],
                        "url": parsed["url"],
                        "pdf_url": parsed["pdf_url"],
                        "doi": parsed["doi"],
                        "department": parsed["department"],
                        "metadata": json.dumps(
                            parsed["metadata"], ensure_ascii=False
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = (
                        f"{saved}/{limit}" if limit is not None else str(saved)
                    )
                    print(
                        f"[{self.site_id}] saved {counter}: "
                        f"{parsed['title'][:90]}"
                    )
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(
                        f"[{self.site_id}] item {item_label} failed: {exc}"
                    )
                    continue

            if (page + 1) % self.PROGRESS_EVERY == 0:
                print(
                    f"[{self.site_id}] page {page}: saved {saved}/"
                    f"{limit_label}"
                )

            if new_in_page == 0:
                # Either an empty page or every URL was already in seen_urls
                # — both signal end-of-pagination for this feed.
                print(
                    f"[{self.site_id}] page {page} produced 0 new records; "
                    "stopping"
                )
                break

            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get_text(self, url, context="request", referer=None, accept=None):
        body, effective_url = self._curl_get_bytes(
            url,
            context=context,
            referer=referer,
            accept=accept
            or (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "application/json,*/*;q=0.8"
            ),
        )
        if body is None:
            return None, effective_url
        return body.decode("utf-8", errors="replace"), effective_url

    def _curl_get_bytes(self, url, context="request", referer=None, accept=None):
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
            f"Accept: {accept or '*/*'}",
            "-H",
            "Accept-Language: en-US,en;q=0.9",
            "-w",
            "\n" + self._CURL_META_MARKER + "%{http_code}\t%{url_effective}",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.CURL_TIMEOUT + 10,
                )
                body, http_code, effective_url = self._split_curl_output(
                    result.stdout or b"", url
                )
                stderr = (result.stderr or b"").decode(
                    "utf-8", errors="replace"
                ).strip()
                if result.returncode != 0:
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if http_code and http_code.isdigit() and int(http_code) >= 400:
                    raise RuntimeError(f"HTTP {http_code} for {effective_url}")
                if not body:
                    raise RuntimeError(f"empty response for {effective_url}")
                return body, effective_url
            except KeyboardInterrupt:
                raise
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

        print(
            f"[{self.site_id}] {context} curl failed after 3 attempts: "
            f"{last_error}"
        )
        return None, url

    def _split_curl_output(self, raw, fallback_url):
        marker = ("\n" + self._CURL_META_MARKER).encode("ascii")
        marker_pos = raw.rfind(marker)
        if marker_pos == -1:
            return raw, "", fallback_url
        body = raw[:marker_pos]
        meta = raw[marker_pos + len(marker):].decode(
            "utf-8", errors="replace"
        ).strip()
        if "\t" not in meta:
            return body, "", fallback_url
        http_code, effective_url = meta.split("\t", 1)
        return body, http_code.strip(), effective_url.strip() or fallback_url

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_list_json(self, raw, context="JSON"):
        """Parse the Drupal Views JSON feed into list-record dicts.

        Returns ``None`` on hard failure (so the outer loop bails) and
        ``[]`` when the feed simply has no items left.
        """
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except Exception as exc:
            print(
                f"[{self.site_id}] JSON parse failed for {context}: {exc}"
            )
            return None
        if not isinstance(data, list):
            print(
                f"[{self.site_id}] unexpected JSON shape for {context}: "
                f"{type(data).__name__}"
            )
            return None

        records = []
        for raw_item in data:
            if not isinstance(raw_item, dict):
                continue
            title_html = raw_item.get("title") or ""
            body_html = raw_item.get("body") or ""
            release_date = raw_item.get("field_release_date") or ""

            title_soup = self._make_soup(title_html, context=f"{context} title")
            if title_soup is None:
                continue
            link = title_soup.find("a", href=True)
            if link is None:
                continue
            href = (link.get("href") or "").strip()
            detail_url = self._clean_url(urljoin(self.base_url, href))
            title = self._normalize_text(link.get_text(" ", strip=True))
            if not detail_url or not title:
                continue

            records.append(
                {
                    "url": detail_url,
                    "title": title,
                    "body_html": body_html,
                    "release_date": release_date,
                }
            )
        return records

    def _parse_detail(self, record):
        body_html = record.get("body_html") or ""
        abstract = self._html_to_text(body_html)
        external_id = self._external_id(record["url"])
        published_date = self._parse_date(record.get("release_date") or "")
        return {
            "external_id": external_id,
            "title": record["title"],
            "authors": ["NINDS"],
            "abstract": abstract[: self.MAX_ABSTRACT_CHARS],
            "category": "Press release",
            "keywords": [],
            "published_date": published_date,
            "url": record["url"],
            "pdf_url": "",
            "doi": "",
            "department": (
                "National Institute of Neurological Disorders and Stroke"
            ),
            "metadata": {
                "source": "Drupal Views JSON feed",
                "listEndpoint": self.LIST_ENDPOINT,
                "startUrl": self.START_URL,
                "releaseDateRaw": record.get("release_date"),
            },
        }

    def _html_to_text(self, html):
        if not html:
            return ""
        soup = self._make_soup(html, context="body")
        if soup is None:
            # Fall back to a regex strip rather than crash.
            stripped = re.sub(r"<[^>]+>", " ", html)
            return self._compact_text(unescape(stripped))
        # Drop script/style noise that can sneak into Drupal body fields.
        for node in soup(["script", "style"]):
            node.decompose()
        text = soup.get_text(" ", strip=True)
        return self._compact_text(text)

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
                print(
                    f"[{self.site_id}] BeautifulSoup({parser}) failed for "
                    f"{context}: {exc}"
                )
        print(
            f"[{self.site_id}] all BeautifulSoup parsers failed for "
            f"{context}: {last_exc}"
        )
        return None

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    _MONTHS = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
        "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
        "november": 11, "december": 12,
    }

    def _parse_date(self, raw):
        raw = self._normalize_text(raw)
        if not raw:
            return ""
        # Handles "Tuesday, February 10, 2026" and "February 10, 2026".
        match = re.search(
            r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})",
            raw,
        )
        if match:
            month_name = match.group(1).lower()
            day = int(match.group(2))
            year = int(match.group(3))
            month = self._MONTHS.get(month_name)
            if month:
                return f"{year:04d}-{month:02d}-{day:02d}"
        match = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
        if match:
            return "-".join(match.groups())
        return ""

    def _external_id(self, url):
        path = urlparse(url).path.rstrip("/")
        slug = path.rsplit("/", 1)[-1]
        return slug or path or url

    def _clean_url(self, url):
        url = (url or "").strip()
        if not url:
            return ""
        return url.split("#", 1)[0]

    def _normalize_text(self, text):
        if text is None:
            return ""
        text = unescape(str(text)).replace("\xa0", " ")
        text = unicodedata.normalize("NFKC", text)
        text = text.replace("\r", "\n")
        return re.sub(r"\s+", " ", text).strip()

    def _compact_text(self, text):
        text = self._normalize_text(text)
        return re.sub(r"\s+", " ", text).strip()
