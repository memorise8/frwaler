# -*- coding: utf-8 -*-
"""Crawler for bj.admin.ch (Federal Office of Justice) press releases.

The site was rebuilt on a Nuxt SPA + the shared admin.ch "nsbc" news
microservice (d-nsbc-p.admin.ch). The old AEM `nsbnewslist.entries.html`
endpoint (still hardcoded here as of the previous version) now 404s.
The SPA itself calls a public JSON search API which already returns full
title/description/body/date per item, so we query that directly instead
of scraping list + detail HTML pages.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urljoin

from crawler.base_crawler import BaseCrawler

_CURL_META_MARKER = "__BJ_ADMIN_CH_BJ_CURL_META__:"
_BACKOFF = (1, 3, 9)
_CURL_TIMEOUT = 45
_PAGE_SIZE = 50
_PAGE_SAFETY_CAP = 200
_MIN_ABSTRACT_CHARS = 50
_WALL_CLOCK_BUDGET_S = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", str(_PAGE_SAFETY_CAP)))
_START_DATE_ISO = "1990-01-01T00:00:00.000Z"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


class BjAdminChBjCrawler(BaseCrawler):
    site_id = "bj-admin-ch-bj"
    site_name = "Custom: bj-admin-ch-bj"
    base_url = "https://www.bj.admin.ch"

    _API_ENDPOINT = "https://d-nsbc-p.admin.ch/v1/search"
    _API_PARAMS_BASE = {
        "languages": "en",
        "newsKinds": ["CONTENT_HUB", "ONSB"],
        "publisherIDs": "29",
        "sort": "DESC",
    }

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self._detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        offset = 0
        seen_urls: set[str] = set()
        seen_pages = 0
        total_available = None
        _t0 = time.monotonic()

        while True:
            # Wall-clock budget guard
            if time.monotonic() - _t0 > _WALL_CLOCK_BUDGET_S:
                print(f"[{self.site_id}] approaching wall-clock budget; stopping early")
                break

            if limit is not None and saved >= limit:
                break

            if seen_pages >= _MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached; stopping")
                break
            seen_pages += 1

            list_url = self._list_url(offset)
            raw = self._curl_get(list_url, context=f"list offset {offset}")
            if not raw:
                print(f"[{self.site_id}] list endpoint failed at offset {offset}; stopping")
                break

            try:
                payload = json.loads(raw)
            except Exception as exc:
                print(f"[{self.site_id}] list offset {offset} JSON parse failed: {exc}; stopping")
                break

            items = payload.get("items") or []
            if total_available is None:
                total_available = payload.get("pageResults")

            if not items:
                print(f"[{self.site_id}] no items at offset {offset}; stopping")
                break

            if offset == 0 or offset % (_PAGE_SIZE * 5) == 0:
                limit_str = str(limit) if limit is not None else "∞"
                print(f"[{self.site_id}] offset {offset}: saved {saved}/{limit_str} "
                      f"(server total={total_available})")

            new_count = 0
            for idx, item in enumerate(items, start=1):
                if limit is not None and saved >= limit:
                    break

                item_label = f"offset {offset} item {idx}"
                try:
                    record = self._build_record(item)
                    if record is None:
                        print(f"[{self.site_id}] item {item_label}: no URL; skipping")
                        continue

                    url = record["url"]
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    new_count += 1

                    abstract = record.get("abstract") or ""
                    if len(abstract) < _MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    paper = {
                        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, url)),
                        "site_id": self.site_id,
                        "external_id": record["external_id"],
                        "post_number": record["post_number"],
                        "title": record["title"],
                        "abstract": abstract,
                        "published_date": record["published_date"],
                        "listed_date": record["listed_date"],
                        "url": url,
                        "pdf_url": None,
                        "doi": None,
                        "authors": None,
                        "publisher": "Federal Office of Justice",
                        "department": "Federal Office of Justice",
                        "journal": None,
                        "category": "Press Release",
                        "keywords": "",
                        "posted_date": record["listed_date"],
                        "original_filename": None,
                        "metadata": json.dumps(record["metadata"], ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {record['title'][:90]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break

            if new_count == 0:
                print(f"[{self.site_id}] offset {offset} had no unseen items; stopping")
                break

            offset += len(items)
            if total_available is not None and offset >= total_available:
                break
            if len(items) < _PAGE_SIZE:
                break

            time.sleep(self._detail_delay)

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _list_url(self, offset: int) -> str:
        now = datetime.now(timezone.utc) + timedelta(days=400)
        params = {
            **self._API_PARAMS_BASE,
            "start_date": _START_DATE_ISO,
            "end_date": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "offset": str(offset),
            "limit": str(_PAGE_SIZE),
        }
        return f"{self._API_ENDPOINT}?{urlencode(params, doseq=True)}"

    def _curl_get(self, url: str, context: str = "request") -> str | None:
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--connect-timeout", "15",
            "--max-time", str(_CURL_TIMEOUT),
            "-A", self.USER_AGENT,
            "-H", "Accept: application/json,text/html;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            "-w", "\n" + _CURL_META_MARKER + "%{http_code}\t%{url_effective}",
            url,
        ]
        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=_CURL_TIMEOUT + 10)
                stdout = (result.stdout or b"").decode("utf-8", errors="replace")
                body, http_code, _ = self._split_curl_output(stdout, url)
                if result.returncode != 0:
                    stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if http_code and int(http_code) >= 400:
                    raise RuntimeError(f"HTTP {http_code}")
                if not body.strip():
                    raise RuntimeError("empty response")
                return body
            except Exception as exc:
                last_error = str(exc)
                print(f"[{self.site_id}] {context} curl failed (attempt {attempt}/3): {last_error}")
                if attempt < 3:
                    wait = _BACKOFF[attempt - 1]
                    print(f"[{self.site_id}] retrying in {wait}s...")
                    time.sleep(wait)
        print(f"[{self.site_id}] {context} failed after 3 attempts: {last_error}")
        return None

    @staticmethod
    def _split_curl_output(raw: str, fallback_url: str):
        marker_pos = raw.rfind("\n" + _CURL_META_MARKER)
        if marker_pos == -1:
            return raw, "", fallback_url
        body = raw[:marker_pos]
        meta = raw[marker_pos + 1 + len(_CURL_META_MARKER):].strip()
        if "\t" not in meta:
            return body, "", fallback_url
        http_code, effective_url = meta.split("\t", 1)
        return body, http_code.strip(), (effective_url.strip() or fallback_url)

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _build_record(self, item: dict) -> dict | None:
        content = item.get("content") or {}
        meta = content.get("metadata") or {}
        systemdata = content.get("systemdata") or {}

        slug = meta.get("slug") or item.get("id") or ""
        if not slug:
            return None
        url = urljoin(self.base_url, f"/en/newnsb/{slug}")

        title = item.get("title") or meta.get("title") or meta.get("metaTitle") or ""

        abstract = (
            item.get("description")
            or meta.get("description")
            or meta.get("metaDescription")
            or ""
        ).strip()
        if len(abstract) < _MIN_ABSTRACT_CHARS:
            abstract = self._abstract_from_text(item.get("text"), fallback=abstract)

        published_date = (
            self._iso_date(item.get("publishDate"))
            or self._iso_date(systemdata.get("firstPublicationDate"))
            or self._iso_date(meta.get("announcementDate"))
        )
        listed_date = (
            self._iso_date(systemdata.get("visiblePublicationDate")) or published_date
        )

        external_id = str(systemdata.get("documentId") or item.get("id") or slug)
        post_number = external_id

        metadata = {
            "posted_date": listed_date,
            "listed_date": listed_date,
            "external_id": external_id,
            "post_number": post_number,
            "node_id": external_id,
            "detail_url": url,
            "news_category": item.get("newsCategory"),
            "location": item.get("location"),
            "publishers": item.get("publishers"),
            "co_publishers": item.get("coPublishers"),
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "source": "bj.admin.ch nsbc search API (d-nsbc-p.admin.ch/v1/search)",
        }

        return {
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "url": url,
            "metadata": metadata,
        }

    @staticmethod
    def _abstract_from_text(text_fragments, fallback: str = "") -> str:
        if not text_fragments:
            return fallback
        parts = []
        total_len = len(fallback)
        for frag in text_fragments:
            if not isinstance(frag, str):
                continue
            plain = _WS_RE.sub(" ", _TAG_RE.sub(" ", frag)).strip()
            if not plain:
                continue
            parts.append(plain)
            total_len += len(plain) + 1
            if total_len >= _MIN_ABSTRACT_CHARS * 3:
                break
        if not parts:
            return fallback
        combined = (fallback + " " + " ".join(parts)).strip() if fallback else " ".join(parts)
        return combined

    @staticmethod
    def _iso_date(value) -> str:
        if not value or not isinstance(value, str):
            return ""
        m = re.match(r"(\d{4}-\d{2}-\d{2})", value)
        return m.group(1) if m else ""
