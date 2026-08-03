# -*- coding: utf-8 -*-
"""Bentham Science press release crawler.

Starting URL: https://www.benthamscience.com/press-release

The site currently renders the listing and detail pages as HTML:
- List:   /press-release?page=N, with <news-list> entries.
- Detail: /press-release-detail/{id}, with <article-title>, <aff>, and body.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

# Absolute import: spec_from_file_location loads this file with no package.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402


_SITE_ID = "benthamscience-com-press-release"
_BASE_URL = "https://www.benthamscience.com"
_LIST_URL = f"{_BASE_URL}/press-release"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_WALL_STOP_GRACE_SECONDS = 20
_BACKOFF_SECONDS = (1, 3, 9)


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = value.replace("\xa0", " ")
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in value.splitlines()]
    lines = [line for line in lines if line]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _text(el: Any) -> str:
    if el is None:
        return ""
    try:
        return _clean_text(el.get_text("\n", strip=True))
    except Exception:
        return ""


def _make_soup(raw: str):
    """Parse HTML with fallback chain: html5lib -> lxml -> html.parser."""
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:
        print(f"[{_SITE_ID}] BeautifulSoup import failed: {exc}")
        return None

    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw or "", parser)
        except Exception as exc:
            print(f"[{_SITE_ID}] parser {parser} failed: {exc}")
            continue
    return None


def _parse_date(raw: str | None) -> str | None:
    if not raw:
        return None
    text = _clean_text(raw)
    text = re.sub(r"(?i)^press\s+release\s+date\s*:\s*", "", text).strip()
    text = re.sub(r"\s+", " ", text)
    for fmt in (
        "%Y-%B-%d",
        "%Y-%b-%d",
        "%d-%B-%Y",
        "%d-%b-%Y",
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%B %d, %Y",
        "%b %d, %Y",
    ):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if match:
        year, month, day = match.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    return None


def _native_id_from_url(url: str) -> str | None:
    match = re.search(r"/press-release-detail/([^/?#]+)", url or "")
    return match.group(1) if match else None


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = unquote(urlparse(url).path or "")
    name = os.path.basename(path.rstrip("/"))
    if "." in name and len(name) <= 200:
        return name
    return None


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        key = re.sub(r"\s+", " ", value or "").strip()
        if not key or key.lower() in seen:
            continue
        seen.add(key.lower())
        result.append(key)
    return result


class BenthamScienceComPressReleaseCrawler(BaseCrawler):
    site_id = "benthamscience-com-press-release"
    site_name = "Custom: benthamscience-com-press-release"
    base_url = "https://www.benthamscience.com"

    def _curl_get(self, url: str, retries: int = 3) -> tuple[str, str]:
        """Fetch a URL with curl and exponential backoff.

        Returns (final_url, body). On repeated failure returns ("", "").
        """
        marker = b"\n__BENTHAM_CURL_META__:"
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    [
                        "curl",
                        "--tls-max",
                        "1.3",
                        "-skL",
                        "--compressed",
                        "-A",
                        self.USER_AGENT,
                        "-H",
                        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
                        "-H",
                        "Accept-Language: en-US,en;q=0.9",
                        "-w",
                        "\n__BENTHAM_CURL_META__:%{http_code}|%{url_effective}",
                        url,
                    ],
                    capture_output=True,
                    timeout=60,
                )
                stdout = result.stdout or b""
                body_bytes = stdout
                status = ""
                final_url = url
                if marker in stdout:
                    body_bytes, meta_bytes = stdout.rsplit(marker, 1)
                    meta = meta_bytes.decode("utf-8", errors="replace").strip()
                    if "|" in meta:
                        status, final_url = meta.split("|", 1)
                    else:
                        status = meta

                body = body_bytes.decode("utf-8", errors="replace")
                if result.returncode == 0 and status.startswith("2") and body.strip():
                    return final_url.strip() or url, body

                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                print(
                    f"[{self.site_id}] curl error (attempt {attempt + 1}/{retries}) "
                    f"{url}: code={status or '?'} rc={result.returncode} {stderr[:200]}"
                )
            except subprocess.TimeoutExpired as exc:
                print(f"[{self.site_id}] curl timeout (attempt {attempt + 1}/{retries}) {url}: {exc}")
            except Exception as exc:
                print(f"[{self.site_id}] curl failed (attempt {attempt + 1}/{retries}) {url}: {exc}")

            if attempt < retries - 1:
                time.sleep(_BACKOFF_SECONDS[attempt])

        print(f"[{self.site_id}] gave up after {retries} attempts: {url}")
        return "", ""

    def _list_url(self, page: int) -> str:
        if page <= 1:
            return _LIST_URL
        return f"{_LIST_URL}?page={page}"

    def _fetch_list_page(self, page: int) -> tuple[list[dict[str, Any]], bool]:
        final_url, raw = self._curl_get(self._list_url(page))
        if not raw:
            return [], False

        soup = _make_soup(raw)
        if soup is None:
            print(f"[{self.site_id}] page {page}: no usable HTML parser")
            return [], False

        items = []
        seen_on_page = set()
        anchors = soup.select('news-list a[href*="/press-release-detail/"]')
        if not anchors:
            anchors = soup.select('a[href*="/press-release-detail/"]')

        for anchor in anchors:
            href = anchor.get("href") or ""
            detail_url = urljoin(final_url or _BASE_URL, href)
            if detail_url in seen_on_page:
                continue
            seen_on_page.add(detail_url)

            title_el = anchor.find("news-title")
            date_el = anchor.find("publication-date")
            raw_date = _text(date_el)
            title = _text(title_el) or _clean_text(anchor.get_text(" ", strip=True).replace(raw_date, ""))
            native_id = _native_id_from_url(detail_url)

            items.append(
                {
                    "url": detail_url,
                    "title": title,
                    "listed_date_raw": raw_date,
                    "listed_date": _parse_date(raw_date),
                    "external_id": native_id,
                    "post_number": native_id,
                }
            )

        has_next = bool(soup.select_one('a[rel="next"]'))
        if not has_next:
            for link in soup.select("ul.pagination a.page-link[href]"):
                if (link.get("href") or "").find(f"page={page + 1}") >= 0:
                    has_next = True
                    break

        return items, has_next

    def _extract_detail_date_raw(self, soup) -> str:
        aff = soup.find("aff")
        aff_text = _text(aff)
        if aff_text:
            match = re.search(r"(?i)press\s+release\s+date\s*:\s*(.+)$", aff_text)
            return _clean_text(match.group(1) if match else aff_text)
        body_text = _text(soup)
        match = re.search(r"(?i)press\s+release\s+date\s*:\s*([0-9]{1,2}-[A-Za-z]+-[0-9]{4})", body_text)
        return _clean_text(match.group(1)) if match else ""

    def _extract_pdf_url(self, body_el: Any) -> str | None:
        if body_el is None:
            return None
        for link in body_el.find_all("a", href=True):
            href = link.get("href") or ""
            href_no_query = href.split("?", 1)[0].lower()
            if href_no_query.endswith(".pdf"):
                return urljoin(_BASE_URL, href)
        return None

    def _extract_journals(self, body_el: Any) -> list[str]:
        if body_el is None:
            return []

        outside_table = []
        all_journals = []
        for link in body_el.find_all("a", href=True):
            href = link.get("href") or ""
            if "/journal/" not in href:
                continue
            name = _clean_text(link.get_text(" ", strip=True))
            if not name:
                continue
            all_journals.append(name)
            if link.find_parent("table") is None:
                outside_table.append(name)

        journals = _dedupe(outside_table) or _dedupe(all_journals)
        return journals[:10]

    def _fetch_detail(self, item: dict[str, Any]) -> dict[str, Any] | None:
        detail_url = item["url"]
        final_url, raw = self._curl_get(detail_url)
        if not raw:
            return None

        soup = _make_soup(raw)
        if soup is None:
            print(f"[{self.site_id}] item {detail_url} failed: no usable HTML parser")
            return None

        title = _text(soup.find("article-title")) or item.get("title") or ""
        raw_detail_date = self._extract_detail_date_raw(soup)
        published_date = _parse_date(raw_detail_date) or item.get("listed_date")

        body_el = soup.select_one(".rt .card-body")
        abstract = _text(body_el)
        if not abstract:
            card = soup.select_one(".rt .card")
            abstract = _text(card)

        pdf_url = self._extract_pdf_url(body_el)
        original_filename = _filename_from_url(pdf_url)
        journals = self._extract_journals(body_el)

        external_id = item.get("external_id") or _native_id_from_url(final_url) or _native_id_from_url(detail_url)
        post_number = item.get("post_number") or external_id
        metadata = {
            "posted_date": item.get("listed_date_raw") or raw_detail_date or item.get("listed_date"),
            "originalFilename": original_filename,
            "journal_raw": journals,
            "series": None,
            "volume": None,
            "issue": None,
            "press_release_id": external_id,
            "node_id": external_id,
            "post_number": post_number,
            "list_title": item.get("title"),
            "list_date_raw": item.get("listed_date_raw"),
            "detail_date_raw": raw_detail_date,
            "listed_date": item.get("listed_date"),
            "published_date": published_date,
            "detail_url": final_url or detail_url,
            "body_length": len(abstract),
        }

        return {
            "id": f"{self.site_id}-{external_id or post_number or abs(hash(detail_url))}",
            "site_id": self.site_id,
            "external_id": str(external_id) if external_id is not None else detail_url,
            "post_number": str(post_number) if post_number is not None else None,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": item.get("listed_date") or published_date,
            "posted_date": item.get("listed_date") or published_date,
            "authors": None,
            "publisher": "Bentham Science Publishers",
            "department": None,
            "journal": "; ".join(journals) if journals else None,
            "url": final_url or detail_url,
            "pdf_url": pdf_url,
            "keywords": None,
            "category": "Press Release",
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _near_wall_limit(self, start_time: float) -> bool:
        elapsed = time.time() - start_time
        return elapsed >= (_MAX_WALL_SECONDS - _WALL_STOP_GRACE_SECONDS)

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls = set()
        start_time = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

        try:
            while True:
                if limit is not None and saved >= limit:
                    break
                if page > _MAX_PAGES:
                    print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached")
                    break
                if self._near_wall_limit(start_time):
                    elapsed = time.time() - start_time
                    print(f"[{self.site_id}] wall-clock budget approaching ({elapsed:.0f}s); stopping cleanly")
                    break

                try:
                    items, has_next = self._fetch_list_page(page)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] page {page} failed: {exc}")
                    break

                if not items:
                    print(f"[{self.site_id}] page {page}: no records; stopping")
                    break

                new_items = [item for item in items if item.get("url") not in seen_urls]
                if not new_items:
                    print(f"[{self.site_id}] page {page}: 0 new records; stopping")
                    break

                for item in new_items:
                    detail_url = item.get("url") or ""
                    seen_urls.add(detail_url)

                    if limit is not None and saved >= limit:
                        break
                    if self._near_wall_limit(start_time):
                        elapsed = time.time() - start_time
                        print(f"[{self.site_id}] wall-clock budget approaching ({elapsed:.0f}s); stopping cleanly")
                        break

                    try:
                        if self._delay:
                            time.sleep(self._delay)
                        paper = self._fetch_detail(item)
                        if not paper:
                            print(f"[{self.site_id}] item {detail_url} failed: empty detail")
                            continue

                        abstract = paper.get("abstract") or ""
                        if len(abstract) < 50:
                            print(
                                f"[{self.site_id}] item {detail_url}: abstract too short "
                                f"({len(abstract)} chars); skipping"
                            )
                            continue

                        if not paper.get("title"):
                            print(f"[{self.site_id}] item {detail_url}: no title; skipping")
                            continue

                        self._save_paper(paper)
                        saved += 1
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[{self.site_id}] item {detail_url} failed: {exc}")
                        continue

                if page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

                if limit is not None and saved >= limit:
                    break
                if self._near_wall_limit(start_time):
                    elapsed = time.time() - start_time
                    print(f"[{self.site_id}] wall-clock budget approaching ({elapsed:.0f}s); stopping cleanly")
                    break
                if not has_next:
                    print(f"[{self.site_id}] page {page}: no next page; stopping")
                    break

                page += 1

        except KeyboardInterrupt:
            raise

        print(f"[{self.site_id}] done: saved {saved} items")
        return saved
