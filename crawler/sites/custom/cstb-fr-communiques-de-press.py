# -*- coding: utf-8 -*-
"""CSTB (Centre Scientifique et Technique du Batiment) press releases crawler.

List source:
    https://www.cstb.fr/communiques-de-presse

The list page is server-rendered on first load, but "load more" / pagination
is driven by a jQuery ``$.post`` back to the same URL with form fields
``{ctg, take, page, triDate}`` (see the inline ``azPress()`` script on the
page). The POST response replaces ``#pressreleases`` with a fresh HTML
fragment -- no CSRF token required. We use that same POST endpoint directly
instead of scraping the initial page, since it lets us request larger pages
(``take``) and iterate ``page`` until an empty/partial page is returned.

Each list item links to a detail page. The detail page's body text lives in
one or more ``div.fr-view`` blocks inside ``div.PageDetail`` (the article's
own content, as opposed to unrelated sidebar/accordion blocks elsewhere on
the page). Some detail pages additionally carry a real downloadable PDF
(often behind a "Télécharger ..." link) -- that becomes ``pdf_url``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

# absolute import -- spec_from_file_location has no package context
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
from crawler.base_crawler import BaseCrawler


_SITE_ID = "cstb-fr-communiques-de-press"
_BASE_URL = "https://www.cstb.fr"
_LIST_URL = f"{_BASE_URL}/communiques-de-presse"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_RETRY_DELAYS = (1, 3, 9)
_MIN_ABSTRACT_CHARS = 50
_TAKE = 36
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _make_soup(raw_html):
    """Parse HTML with html5lib -> lxml -> html.parser fallback chain."""
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:
        print(f"[{_SITE_ID}] BeautifulSoup unavailable: {exc}")
        return None

    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw_html or "", parser)
        except Exception as exc:
            print(f"[{_SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
            continue
    return None


def _clean_text(value):
    if value is None:
        return ""
    value = str(value).replace("\x00", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def _json_dumps(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _date_from_ddmmyyyy(raw):
    if not raw:
        return None
    match = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", str(raw))
    if not match:
        return None
    day, month, year = match.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}"


def _slug_from_url(url):
    path = urllib.parse.urlsplit(url or "").path
    return path.rstrip("/").rsplit("/", 1)[-1] or None


def _filename_from_url(url):
    path = urllib.parse.urlsplit(url or "").path
    filename = urllib.parse.unquote(path.rstrip("/").rsplit("/", 1)[-1])
    return filename or None


class CstbFrCommuniquesDePressCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: cstb-fr-communiques-de-press"
    base_url = _BASE_URL

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # HTTP helpers (curl-based, TLS 1.3 max for parity with other crawlers
    # and 3x exponential-backoff retries)
    # ------------------------------------------------------------------

    def _fetch_bytes(self, url, *, method="GET", data=None, context=None, max_time=60):
        headers = [
            "-H", f"User-Agent: {_USER_AGENT}",
            "-H", "Accept-Language: fr-FR,fr;q=0.9,en;q=0.8",
        ]
        cmd = ["curl", "--tls-max", "1.3", "-skL", "--max-time", str(max_time), *headers]
        if method == "POST":
            cmd += ["-X", "POST"]
            for key, value in (data or {}).items():
                cmd += ["--data-urlencode", f"{key}={value}"]
        cmd.append(url)

        label = context or url
        for attempt, wait in enumerate(_RETRY_DELAYS, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=max_time + 10)
                if result.returncode == 0 and result.stdout:
                    return result.stdout
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                print(
                    f"[{self.site_id}] curl attempt {attempt}/3 failed for "
                    f"{label}: rc={result.returncode} {stderr[:200]}"
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] curl attempt {attempt}/3 failed for {label}: {exc}")

            if attempt < len(_RETRY_DELAYS):
                time.sleep(wait)

        return None

    def _fetch_text(self, url, *, method="GET", data=None, context=None, max_time=60):
        raw = self._fetch_bytes(url, method=method, data=data, context=context, max_time=max_time)
        if raw is None:
            return None
        return raw.decode("utf-8", errors="replace")

    # ------------------------------------------------------------------
    # Crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        if limit is not None and limit <= 0:
            return 0

        saved = 0
        seen_urls = set()
        limit_or_inf = limit if limit is not None else "inf"
        started_at = time.time()

        for page_num in range(1, _MAX_PAGES + 1):
            if time.time() - started_at >= _BUDGET_SECONDS - 30:
                print(f"[{self.site_id}] wall-clock budget approaching at page {page_num}; stopping")
                break
            if limit is not None and saved >= limit:
                break

            query_page = page_num - 1
            raw = self._fetch_text(
                _LIST_URL,
                method="POST",
                data={"ctg": "*", "take": _TAKE, "page": query_page, "triDate": "desc"},
                context=f"list page {page_num}",
            )
            if raw is None:
                print(f"[{self.site_id}] list page {page_num} failed; stopping")
                break

            soup = _make_soup(raw)
            if soup is None:
                print(f"[{self.site_id}] list page {page_num} unparseable; stopping")
                break

            items = self._parse_list_items(soup)
            if not items:
                print(f"[{self.site_id}] page {page_num}: no records; stopping")
                break
            has_next = len(items) >= _TAKE

            new_on_page = 0
            for item_index, item in enumerate(items, start=1):
                if limit is not None and saved >= limit:
                    break
                if time.time() - started_at >= _BUDGET_SECONDS - 30:
                    print(f"[{self.site_id}] wall-clock budget approaching during page {page_num}; stopping")
                    return saved

                detail_url = item["url"]
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                try:
                    if self._process_item(item):
                        saved += 1
                    time.sleep(self.detail_delay)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {page_num}.{item_index} failed: {exc}; continuing")
                    continue

            if page_num % 10 == 0:
                print(f"[{self.site_id}] page {page_num}: saved {saved}/{limit_or_inf}")

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page_num}: no new items; stopping")
                break
            if not has_next:
                print(f"[{self.site_id}] page {page_num}: last (partial) page reached")
                break
        else:
            print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached")

        return saved

    # ------------------------------------------------------------------
    # List page parsing
    # ------------------------------------------------------------------

    def _parse_list_items(self, soup):
        items = []
        for article in soup.select("article"):
            link = article.select_one(".info a[href]") or article.select_one("a[href]")
            if not link:
                continue
            href = link.get("href") or ""
            if "/communiques-de-presse/" not in href:
                continue
            full_url = urllib.parse.urljoin(self.base_url, href)

            title_el = article.select_one(".info h3") or article.select_one("h3")
            title = _clean_text(title_el.get_text(" ", strip=True)) if title_el else None

            teaser_el = article.select_one(".info p")
            teaser = _clean_text(teaser_el.get_text(" ", strip=True)) if teaser_el else ""

            date_el = article.select_one(".press-date")
            listed_date_raw = _clean_text(date_el.get_text(" ", strip=True)) if date_el else None
            listed_date = _date_from_ddmmyyyy(listed_date_raw)

            atrib_el = article.select_one(".atrib")
            category = _clean_text(atrib_el.get_text(" ", strip=True)) if atrib_el else None

            items.append({
                "url": full_url,
                "list_title": title,
                "teaser": teaser,
                "listed_date": listed_date,
                "listed_date_raw": listed_date_raw,
                "category": category,
            })
        return items

    # ------------------------------------------------------------------
    # Detail page parsing + save
    # ------------------------------------------------------------------

    def _find_pdf_url(self, soup):
        candidates = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if ".pdf" not in href.lower():
                continue
            text = a.get_text(" ", strip=True)
            candidates.append((text, href))
        if not candidates:
            return None
        for text, href in candidates:
            if "télécharg" in text.lower() or "telecharg" in text.lower():
                return urllib.parse.urljoin(self.base_url, href)
        return urllib.parse.urljoin(self.base_url, candidates[0][1])

    def _process_item(self, item):
        detail_url = item["url"]
        raw = self._fetch_text(detail_url, context=f"detail {detail_url}")
        if raw is None:
            print(f"[{self.site_id}] item {detail_url} failed: fetch returned no body")
            return False

        soup = _make_soup(raw)
        if soup is None:
            print(f"[{self.site_id}] item {detail_url} unparseable; skipping")
            return False

        h1 = soup.select_one("h1.h1") or soup.select_one("h1")
        title = _clean_text(h1.get_text(" ", strip=True)) if h1 else item.get("list_title")
        if not title:
            og_title = soup.select_one('meta[property="og:title"]')
            title = _clean_text(og_title.get("content")) if og_title else None
        if not title:
            print(f"[{self.site_id}] item {detail_url} has no title; skipping")
            return False

        subtitle_el = soup.select_one("div.headerDetailCustom h2")
        subtitle = _clean_text(subtitle_el.get_text(" ", strip=True)) if subtitle_el else ""

        page_detail = soup.select_one("div.PageDetail")
        body_parts = []
        if page_detail is not None:
            for block in page_detail.select("div.fr-view"):
                text = _clean_text(block.get_text(" ", strip=True))
                if text:
                    body_parts.append(text)

        abstract_parts = [p for p in ([subtitle] + body_parts) if p]
        abstract = _clean_text(" ".join(abstract_parts))
        if not abstract:
            abstract = item.get("teaser") or ""
        if len(abstract) < _MIN_ABSTRACT_CHARS:
            print(f"[{self.site_id}] item {detail_url} short abstract ({len(abstract)} chars); skipping")
            return False

        time_el = soup.select_one("time")
        time_text = _clean_text(time_el.get_text(" ", strip=True)) if time_el else None
        published_date = _date_from_ddmmyyyy(time_text) or item.get("listed_date")

        listed_date = item.get("listed_date") or published_date

        guid_el = soup.select_one("[data-node-guid]")
        node_guid = guid_el.get("data-node-guid") if guid_el else None

        slug = _slug_from_url(detail_url)
        external_id = slug or node_guid
        post_number = external_id

        pdf_url = self._find_pdf_url(soup)
        original_filename = _filename_from_url(pdf_url) if pdf_url else None

        category = item.get("category")

        metadata = {
            "posted_date": item.get("listed_date_raw"),
            "published_date_raw": time_text,
            "node_id": node_guid,
            "slug": slug,
            "list_teaser": item.get("teaser"),
            "subtitle": subtitle,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
        }

        paper = {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, detail_url)),
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": None,
            "publisher": "CSTB",
            "department": None,
            "journal": None,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": None,
            "category": category,
            "doi": None,
            "original_filename": original_filename,
            "metadata": _json_dumps(metadata),
        }
        self._save_paper(paper)
        print(f"[{self.site_id}] saved: {title[:60]}")
        return True
