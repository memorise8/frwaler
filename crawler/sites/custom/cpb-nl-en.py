# -*- coding: utf-8 -*-
"""CPB (Netherlands Bureau for Economic Policy Analysis) publications crawler.

Starting URL: https://www.cpb.nl/en/publications

Discovery notes:
  - Drupal site with a Search-API-driven faceted list
    (``data-component-id="cpb:faceted_list"``). No JSON API is exposed
    publicly; the rendered HTML list is the real source of truth.
  - List items: ``div[data-component-id="cpb:overview_list_item"]`` -> 10
    items per page. Pagination is Drupal's standard pager: ``?page={N}``
    (0-indexed). Last page has no ``pager__item--next`` element.
  - Detail pages carry rich ``<meta>`` tags: ``contenttype``,
    ``publicationyear``, ``publicationdatetime`` (ISO 8601),
    ``author`` (comma-separated), ``og:description`` (clean abstract text),
    ``og:url`` (canonical). Node id lives on
    ``<article data-history-node-id="...">``.
  - Downloads (PDF/XLSX/...) live under
    ``section[aria-label="Downloads"] a[download]``.
  - Not every publication has a byline author; some (e.g. World Trade
    Monitor releases) rely on staff "contact person" cards instead.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

# Absolute import - spec_from_file_location has no package context.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from crawler.base_crawler import BaseCrawler


_START_URL = "https://www.cpb.nl/en/publications"
_PAGE_CAP = 200
_CRAWL_BUDGET_SECONDS = 25 * 60
_RETRY_WAITS = (1, 3, 9)

_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def _clean_text(value):
    if not value:
        return ""
    text = unescape(value)
    text = unescape(text)
    text = text.replace("\xa0", " ").replace("​", "")
    text = text.replace("‐", "-").replace("‑", "-")
    return re.sub(r"\s+", " ", text).strip(" \t\r\n,;")


def _make_soup(raw):
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup

            return BeautifulSoup(raw or "", parser)
        except Exception as exc:
            print(f"[cpb-nl-en] BeautifulSoup parser {parser} failed: {exc}")
            continue
    return None


def _parse_human_date(raw):
    """Parse dates like 'June 30, 2026' -> '2026-06-30'."""
    value = _clean_text(raw)
    if not value:
        return None

    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
    if match:
        return match.group(0)

    match = re.search(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", value)
    if match:
        month = _MONTHS.get(match.group(1).lower())
        if month:
            return f"{match.group(3)}-{month:02d}-{int(match.group(2)):02d}"
    return None


def _parse_iso_datetime(raw):
    value = _clean_text(raw)
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return _parse_human_date(value)


def _slug_from_url(url):
    if not url:
        return None
    tail = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return unquote(tail) if tail else None


def _filename_from_url(url):
    if not url:
        return None
    tail = unquote(urlparse(url).path.rstrip("/").split("/")[-1])
    if tail and "." in tail:
        return tail[:240]
    return None


def _dedupe_join(values, sep="; "):
    seen = set()
    out = []
    for value in values:
        cleaned = _clean_text(value)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return sep.join(out) if out else None


class CpbNlEnCrawler(BaseCrawler):
    site_id = "cpb-nl-en"
    site_name = "Custom: cpb-nl-en"
    base_url = "https://www.cpb.nl"

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _curl_get(self, url, referer=None):
        headers = [
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
        ]
        if referer:
            headers.extend(["-H", f"Referer: {referer}"])

        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--max-time", "45", *headers, url,
        ]

        for attempt, wait in enumerate(_RETRY_WAITS, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=55)
                if result.stdout:
                    return result.stdout.decode("utf-8", errors="replace")
                err = result.stderr.decode("utf-8", errors="replace").strip()
                print(f"[{self.site_id}] empty response attempt {attempt}/3 for {url}: {err}")
            except Exception as exc:
                print(f"[{self.site_id}] curl error attempt {attempt}/3 for {url}: {exc}")

            if attempt < 3:
                time.sleep(wait)

        print(f"[{self.site_id}] fetch failed after 3 attempts: {url}")
        return None

    # ------------------------------------------------------------------
    # List page
    # ------------------------------------------------------------------

    def _list_url(self, page):
        # Drupal pager is 0-indexed; our `page` counter is 1-indexed.
        return f"{self.base_url}/en/publications?page={page - 1}"

    def _fetch_list_page(self, page):
        url = self._list_url(page)
        raw = self._curl_get(url, referer=_START_URL)
        if not raw:
            return [], False

        soup = _make_soup(raw)
        if soup is None:
            return [], False

        items = []
        for block in soup.select('div[data-component-id="cpb:overview_list_item"]'):
            link = block.select_one("a.overview-list-item__link")
            if not link or not link.get("href"):
                continue
            detail_url = urljoin(self.base_url, link.get("href"))

            heading = block.select_one("h2.overview-list-item__heading")
            title = _clean_text(heading.get_text(" ", strip=True)) if heading else ""

            subheading = block.select_one("p.overview-list-item__subheading")
            raw_date = _clean_text(subheading.get_text(" ", strip=True)) if subheading else None

            body = block.select_one("p.overview-list-item__body-text")
            teaser = _clean_text(body.get_text(" ", strip=True)) if body else None

            tag_el = block.select_one("span.overview-list-item__tag")
            tag = _clean_text(tag_el.get_text(" ", strip=True)) if tag_el else None

            items.append({
                "detail_url": detail_url,
                "title": title,
                "raw_list_date": raw_date,
                "listed_date": _parse_human_date(raw_date),
                "teaser": teaser,
                "tag": tag,
            })

        has_next = soup.select_one("li.pager__item--next a") is not None
        return items, has_next

    # ------------------------------------------------------------------
    # Detail page
    # ------------------------------------------------------------------

    @staticmethod
    def _meta_map(soup):
        meta = {}
        for tag in soup.find_all("meta"):
            name = tag.get("name")
            if name and tag.get("content") is not None:
                meta[name] = tag.get("content")
        return meta

    @staticmethod
    def _extract_node_id(raw):
        match = re.search(r'data-history-node-id="(\d+)"', raw or "")
        return match.group(1) if match else None

    @staticmethod
    def _extract_authors_from_contacts(soup):
        names = []
        for card in soup.select("div.contact-person"):
            name_el = card.select_one("span.font-bold")
            if name_el:
                names.append(name_el.get_text(" ", strip=True))
        return _dedupe_join(names)

    @staticmethod
    def _extract_abstract(soup, meta):
        text_el = soup.select_one(".page-introduction__text")
        if text_el:
            paragraphs = [_clean_text(p.get_text(" ", strip=True)) for p in text_el.find_all("p")]
            paragraphs = [p for p in paragraphs if p]
            if paragraphs:
                joined = "\n\n".join(paragraphs)
                if len(joined) >= 50:
                    return joined
        og_desc = _clean_text(meta.get("og:description"))
        if len(og_desc) >= 50:
            return og_desc
        if text_el:
            fallback = _clean_text(text_el.get_text(" ", strip=True))
            if fallback:
                return fallback
        return og_desc

    @staticmethod
    def _extract_downloads(soup):
        downloads = []
        section = soup.select_one('section[aria-label="Downloads"]')
        if not section:
            return downloads
        seen = set()
        for link in section.select("a[href][download]"):
            href = link.get("href")
            if not href or href in seen:
                continue
            seen.add(href)
            downloads.append({
                "url": urljoin("https://www.cpb.nl", href),
                "label": _clean_text(link.get_text(" ", strip=True)) or None,
            })
        return downloads

    def _parse_detail(self, url, list_item):
        raw = self._curl_get(url, referer=_START_URL)
        if not raw:
            return None
        soup = _make_soup(raw)
        if soup is None:
            return None

        meta = self._meta_map(soup)

        title_el = soup.select_one("h1.title-block__title")
        title = _clean_text(title_el.get_text(" ", strip=True)) if title_el else list_item.get("title")
        if not title:
            return None

        node_id = self._extract_node_id(raw)
        canonical = _clean_text(meta.get("og:url")) or url
        slug = _slug_from_url(canonical)
        external_id = node_id or slug
        post_number = node_id if node_id and node_id.isdigit() else slug

        published_date = _parse_iso_datetime(meta.get("publicationdatetime"))
        secondary_el = soup.select_one("p.title-block__secondary-text")
        raw_detail_date = _clean_text(secondary_el.get_text(" ", strip=True)) if secondary_el else None
        listed_date = (
            list_item.get("listed_date")
            or _parse_human_date(raw_detail_date)
            or published_date
        )
        if not published_date:
            published_date = listed_date

        abstract = self._extract_abstract(soup, meta)
        if len(abstract) < 50:
            print(f"[{self.site_id}] item {url} skipped: abstract too short ({len(abstract)} chars)")
            return None

        meta_authors = _dedupe_join((meta.get("author") or "").split(","))
        authors = meta_authors or self._extract_authors_from_contacts(soup)

        downloads = self._extract_downloads(soup)
        pdf_url = None
        for item in downloads:
            if item["url"].lower().endswith(".pdf"):
                pdf_url = item["url"]
                break
        original_filename = _filename_from_url(pdf_url)

        category = _clean_text(meta.get("contenttype")) or list_item.get("tag") or None
        keywords = _clean_text(meta.get("theme")) or None

        metadata = {
            "posted_date": list_item.get("raw_list_date") or raw_detail_date,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "node_id": node_id,
            "contenttype": meta.get("contenttype"),
            "theme": meta.get("theme") or None,
            "publicationyear": meta.get("publicationyear"),
            "publicationtimestamp": meta.get("publicationtimestamp"),
            "downloads": downloads,
            "list_tag": list_item.get("tag"),
            "list_teaser": list_item.get("teaser"),
            "source_list_endpoint": "/en/publications?page={N}",
            "source_detail_endpoint": canonical,
        }

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": str(external_id) if external_id else canonical,
            "post_number": str(post_number) if post_number else None,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": authors,
            "publisher": "CPB | Netherlands Bureau for Economic Policy Analysis",
            "department": None,
            "journal": None,
            "url": canonical,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls = set()
        started = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

        while page <= _PAGE_CAP:
            if limit is not None and saved >= limit:
                break
            if time.time() - started >= _CRAWL_BUDGET_SECONDS - 30:
                print(f"[{self.site_id}] wall-clock budget nearly reached; stopping cleanly.")
                break
            if page == _PAGE_CAP:
                print(f"[{self.site_id}] safety cap of {_PAGE_CAP} pages reached.")

            items, has_next = self._fetch_list_page(page)
            if not items:
                print(f"[{self.site_id}] page {page}: no records found. Done.")
                break

            new_items = []
            for item in items:
                detail_url = item.get("detail_url")
                if not detail_url or detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_items.append(item)

            if not new_items:
                print(f"[{self.site_id}] page {page}: all records already seen. Done.")
                break

            for idx, item in enumerate(new_items, start=1):
                if limit is not None and saved >= limit:
                    break
                if time.time() - started >= _CRAWL_BUDGET_SECONDS - 30:
                    print(f"[{self.site_id}] wall-clock budget nearly reached; stopping cleanly.")
                    return saved

                detail_url = item.get("detail_url")
                try:
                    time.sleep(self._delay)
                    paper = self._parse_detail(detail_url, item)
                    if not paper:
                        print(f"[{self.site_id}] item {detail_url} failed: no detail data")
                        continue
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {detail_url} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue
                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_or_inf}: {paper.get('title', '')[:70]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    item_name = detail_url or f"page {page} item {idx}"
                    print(f"[{self.site_id}] item {item_name} failed: {exc}")
                    continue

            if page % 10 == 0:
                print(f"[cpb-nl-en] page {page}: saved {saved}/{limit_or_inf}")

            if not has_next:
                print(f"[{self.site_id}] page {page}: next page link absent. Done.")
                break

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
