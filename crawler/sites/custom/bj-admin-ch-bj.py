# -*- coding: utf-8 -*-
"""Crawler for bj.admin.ch (Federal Office of Justice) press releases."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from datetime import datetime
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

_CURL_META_MARKER = "__BJ_ADMIN_CH_BJ_CURL_META__:"
_BACKOFF = (1, 3, 9)
_CURL_TIMEOUT = 45
_ITEMS_PER_PAGE = 20
_PAGE_SAFETY_CAP = 200
_MIN_ABSTRACT_CHARS = 50
_WALL_CLOCK_BUDGET_S = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))


class BjAdminChBjCrawler(BaseCrawler):
    site_id = "bj-admin-ch-bj"
    site_name = "Custom: bj-admin-ch-bj"
    base_url = "https://www.bj.admin.ch"

    _LIST_ENDPOINT = (
        "https://www.bj.admin.ch/bj/en/home/aktuell/mm"
        "/_jcr_content/nsbnewslist.entries.html"
    )
    _LIST_PARAMS_BASE = {
        "startDate": "01.03.2016",
        "endDate": "01.04.2026",
        "organization": "403",
        "topic": "",
    }

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self._detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 0
        seen_urls: set[str] = set()
        _t0 = time.monotonic()

        while True:
            # Wall-clock budget guard
            if time.monotonic() - _t0 > _WALL_CLOCK_BUDGET_S:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; stopping early")
                break

            if limit is not None and saved >= limit:
                break

            if page >= _PAGE_SAFETY_CAP:
                print(f"[{self.site_id}] safety cap of {_PAGE_SAFETY_CAP} pages reached; stopping")
                break

            list_url = self._list_url(page)
            raw = self._curl_get(list_url, context=f"list page {page}")
            if not raw:
                print(f"[{self.site_id}] list endpoint failed at page {page}; stopping")
                break

            soup = self._make_soup(raw, context=f"list page {page}")
            if soup is None:
                print(f"[{self.site_id}] list page {page} could not be parsed; stopping")
                break

            records = self._parse_list(soup)
            if not records:
                print(f"[{self.site_id}] no records at page {page}; stopping")
                break

            if page % 10 == 0:
                limit_str = str(limit) if limit is not None else "∞"
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            page_new_urls = 0
            for idx, record in enumerate(records, start=1):
                if limit is not None and saved >= limit:
                    break

                item_label = f"page {page} item {idx}"
                try:
                    detail_url = record.get("url") or ""
                    if not detail_url:
                        print(f"[{self.site_id}] item {item_label}: no URL; skipping")
                        continue
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    page_new_urls += 1

                    time.sleep(self._detail_delay)

                    detail_raw = self._curl_get(
                        detail_url, context=f"item {item_label} detail"
                    )
                    if not detail_raw:
                        raise RuntimeError("detail fetch failed after retries")

                    detail_soup = self._make_soup(
                        detail_raw, context=f"item {item_label} detail"
                    )
                    if detail_soup is None:
                        raise RuntimeError("detail HTML could not be parsed")

                    parsed = self._parse_detail(detail_soup, detail_raw, record)
                    abstract = parsed.get("abstract") or ""
                    if len(abstract) < _MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    paper = {
                        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, parsed["url"])),
                        "site_id": self.site_id,
                        "external_id": parsed["external_id"],
                        "post_number": parsed["post_number"],
                        "title": parsed["title"],
                        "abstract": abstract,
                        "published_date": parsed["published_date"],
                        "listed_date": parsed["listed_date"],
                        "url": parsed["url"],
                        "pdf_url": parsed["pdf_url"],
                        "doi": parsed["doi"],
                        "authors": parsed["authors"],
                        "publisher": parsed["publisher"],
                        "department": parsed["department"],
                        "journal": parsed["journal"],
                        "category": parsed["category"],
                        "keywords": parsed["keywords"],
                        "posted_date": parsed["listed_date"],
                        "original_filename": parsed["original_filename"],
                        "metadata": json.dumps(parsed["metadata"], ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {parsed['title'][:90]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break

            if page_new_urls == 0:
                print(f"[{self.site_id}] page {page} had no unseen URLs; stopping")
                break

            if not self._has_next_page(soup):
                break
            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _list_url(self, page_index: int) -> str:
        params = "&".join(
            f"{k}={v}" for k, v in {**self._LIST_PARAMS_BASE, "pageIndex": str(page_index)}.items()
        )
        return f"{self._LIST_ENDPOINT}?{params}"

    def _curl_get(self, url: str, context: str = "request") -> str | None:
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--connect-timeout", "15",
            "--max-time", str(_CURL_TIMEOUT),
            "-A", self.USER_AGENT,
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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

    def _make_soup(self, raw: str, context: str = "HTML") -> BeautifulSoup | None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                last_exc = exc
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        print(f"[{self.site_id}] all parsers failed for {context}: {last_exc}")
        return None

    def _parse_list(self, soup: BeautifulSoup) -> list[dict]:
        records = []
        seen: set[str] = set()
        for item in soup.select("div.list-group-item"):
            link = item.select_one("h3 a[href], h2 a[href]")
            if link is None:
                continue
            url = link.get("href", "").strip()
            if not url or url in seen:
                continue
            url = urljoin(self.base_url, url)
            seen.add(url)

            title = self._node_text(link)
            if not title:
                continue

            # Date is in <p> sibling before the h3
            date_node = item.select_one("p")
            list_date_raw = self._node_text(date_node)
            listed_date = self._parse_dmy(list_date_raw)

            external_id = self._id_from_url(url)
            records.append({
                "url": url,
                "title": title,
                "external_id": external_id,
                "listed_date": listed_date,
                "list_date_raw": list_date_raw,
            })
        return records

    def _parse_detail(self, soup: BeautifulSoup, raw_html: str, record: dict) -> dict:
        url = record.get("url") or ""

        # Title
        h1 = soup.find(class_=re.compile(r"hero__title"))
        title = self._node_text(h1) or record.get("title", "")

        # Abstract from hero__description
        desc_node = soup.find(class_=re.compile(r"hero__description"))
        abstract = self._node_text(desc_node) if desc_node else ""

        # If not found by class, try meta description
        if len(abstract) < _MIN_ABSTRACT_CHARS:
            meta_desc = self._meta_content(soup, "og:description", "description")
            if len(meta_desc) > len(abstract):
                abstract = meta_desc

        # Date: look in meta-info__item spans for "Published on …"
        published_date = ""
        for span in soup.find_all(class_=re.compile(r"meta-info__item")):
            text = self._node_text(span)
            m = re.match(r"Published on (.+)", text, re.IGNORECASE)
            if m:
                published_date = self._parse_english_date(m.group(1).strip())
                break

        # Fallback: extract date from description text (e.g., "Bern, 8.12.2023 -")
        if not published_date and abstract:
            m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b", abstract)
            if m:
                published_date = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"

        # Fallback to listed_date
        if not published_date:
            published_date = record.get("listed_date") or ""

        # PDF links
        pdf_url = None
        original_filename = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if re.search(r"\.pdf(?:[?#]|$)", href, re.I):
                pdf_url = href if href.startswith("http") else urljoin(self.base_url, href)
                seg = urlparse(pdf_url).path.rstrip("/").split("/")[-1]
                original_filename = seg or None
                break

        external_id = self._id_from_url(url) or record.get("external_id") or url
        post_number = external_id
        listed_date = record.get("listed_date") or published_date

        metadata = {
            "posted_date": listed_date,
            "listed_date": listed_date,
            "list_date_raw": record.get("list_date_raw", ""),
            "originalFilename": original_filename,
            "external_id": external_id,
            "post_number": post_number,
            "node_id": external_id,
            "detail_url": url,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "source": "bj.admin.ch press releases list + admin.ch detail page",
        }

        return {
            "external_id": external_id,
            "post_number": post_number,
            "title": title or record.get("title", ""),
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "url": url,
            "pdf_url": pdf_url,
            "doi": None,
            "authors": None,
            "publisher": "Federal Office of Justice",
            "department": "Federal Office of Justice",
            "journal": None,
            "category": "Press Release",
            "keywords": "",
            "original_filename": original_filename,
            "metadata": metadata,
        }

    def _has_next_page(self, soup: BeautifulSoup) -> bool:
        next_link = soup.find(
            "a",
            attrs={"data-loadpage": True},
            string=re.compile(r"\bNext\b", re.I),
        )
        if next_link and next_link.get("aria-disabled") != "true":
            return True
        for a in soup.find_all("a", attrs={"data-loadpage": True}):
            title = (a.get("title") or "").lower()
            if "last page" in title and a.get("aria-disabled") != "true":
                return True
        return False

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _id_from_url(url: str) -> str:
        """Extract native ID from admin.ch press release URL."""
        parsed = urlparse(url)
        # nsb?id=XXXXX format
        qs = parse_qs(parsed.query)
        if "id" in qs:
            return qs["id"][0]
        # newnsb/SLUG format
        path = parsed.path.rstrip("/")
        if path:
            return unquote(path.split("/")[-1])
        return url

    @staticmethod
    def _parse_dmy(text: str) -> str:
        """Parse DD.MM.YYYY → YYYY-MM-DD."""
        m = re.search(r"(\d{1,2})\.(\d{1,2})\.(20\d{2})", text or "")
        if m:
            return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
        return ""

    @staticmethod
    def _parse_english_date(text: str) -> str:
        """Parse 'DD Month YYYY' or 'Month DD, YYYY' → YYYY-MM-DD."""
        text = text.strip()
        # DD Month YYYY
        m = re.match(r"(\d{1,2})\s+([A-Za-z]+)\s+(20\d{2})", text)
        if m:
            month = _MONTHS_EN.get(m.group(2).lower())
            if month:
                return f"{m.group(3)}-{month:02d}-{int(m.group(1)):02d}"
        # Month DD, YYYY
        m = re.match(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(20\d{2})", text)
        if m:
            month = _MONTHS_EN.get(m.group(1).lower())
            if month:
                return f"{m.group(3)}-{month:02d}-{int(m.group(2)):02d}"
        return ""

    @staticmethod
    def _node_text(node) -> str:
        if node is None:
            return ""
        raw = node.get_text(" ", strip=True)
        raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
        return raw.strip()

    @staticmethod
    def _meta_content(soup: BeautifulSoup, *keys: str) -> str:
        for key in keys:
            node = soup.find("meta", attrs={"name": key})
            if node and node.get("content"):
                return node["content"].strip()
            node = soup.find("meta", attrs={"property": key})
            if node and node.get("content"):
                return node["content"].strip()
        return ""
