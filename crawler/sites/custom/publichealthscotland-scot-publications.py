# -*- coding: utf-8 -*-
"""Crawler for Public Health Scotland publications.

List page: https://www.publichealthscotland.scot/publications/?q=&p=N&sort=
  - Items: li.search-item
  - h3: title; ul.list-inline.text-muted li[0]: listed date; li[1+]: category
  - .search-item-description p: short description

Detail page: /publications/<series>/<release-slug>
  - meta[name=description]: abstract
  - dl dt/dd pairs: Published date, Type, Author
  - a[href$=.pdf]: PDF link
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class PublichealthscotlandScotPublicationsCrawler(BaseCrawler):
    site_id = "publichealthscotland-scot-publications"
    site_name = "Custom: publichealthscotland-scot-publications"
    base_url = "https://www.publichealthscotland.scot"

    LIST_URL = "https://www.publichealthscotland.scot/publications/"
    MIN_ABSTRACT_CHARS = 100
    BACKOFF = (1, 3, 9)
    MAX_PAGES = 200
    TIME_BUDGET_SECS = 25 * 60

    _MONTHS = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        start_time = time.time()

        for page in range(1, self.MAX_PAGES + 1):
            if limit is not None and saved >= limit:
                break

            if time.time() - start_time > self.TIME_BUDGET_SECS:
                print(f"[{self.site_id}] time budget exceeded at page {page}; stopping")
                break

            if page == self.MAX_PAGES - 1:
                print(f"[{self.site_id}] safety cap of {self.MAX_PAGES} pages reached; stopping")

            if page % 10 == 0:
                limit_str = str(limit) if limit is not None else "inf"
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            list_url = f"{self.LIST_URL}?q=&p={page}&sort="
            raw = self._curl_get(list_url, context=f"list page {page}")
            if not raw:
                print(f"[{self.site_id}] list page {page} fetch failed; stopping")
                break

            soup = self._make_soup(raw)
            if soup is None:
                print(f"[{self.site_id}] list page {page} could not be parsed; stopping")
                break

            items = soup.select("li.search-item")
            if not items:
                print(f"[{self.site_id}] no items on list page {page}; stopping")
                break

            any_new = False

            for item in items:
                if limit is not None and saved >= limit:
                    break

                try:
                    a = item.select_one("a[href]")
                    if not a:
                        continue

                    detail_url = urljoin(self.base_url, a["href"])
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    any_new = True

                    # List-level fields
                    h3 = item.select_one("h3")
                    list_title = (
                        self._clean_text(h3) if h3
                        else self._clean_text(a.get("title", ""))
                    )

                    muted_lis = item.select("ul.list-inline.text-muted li")
                    listed_date_raw = muted_lis[0].get_text(strip=True) if muted_lis else ""
                    categories = [
                        li.get_text(strip=True)
                        for li in muted_lis[1:]
                        if li.get_text(strip=True)
                    ]

                    list_desc_el = item.select_one(".search-item-description p")
                    list_desc = self._clean_text(list_desc_el) if list_desc_el else ""

                    listed_date = self._parse_date(listed_date_raw)

                    # Fetch detail page
                    time.sleep(self._delay)
                    detail_raw = self._curl_get(detail_url, context=f"detail {detail_url}")
                    if not detail_raw:
                        raise RuntimeError("detail page fetch failed after retries")

                    detail_soup = self._make_soup(detail_raw)
                    if detail_soup is None:
                        raise RuntimeError("detail page could not be parsed")

                    parsed = self._parse_detail(
                        detail_soup, detail_url,
                        list_title, list_desc,
                        listed_date, listed_date_raw, categories,
                    )

                    abstract = parsed["abstract"]
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item skipped: abstract too short "
                            f"({len(abstract)} chars): {detail_url}"
                        )
                        continue

                    paper = {
                        "site_id": self.site_id,
                        "external_id": parsed["external_id"],
                        "url": detail_url,
                        "title": parsed["title"],
                        "abstract": abstract,
                        "authors": parsed["authors"],
                        "publisher": parsed["publisher"],
                        "published_date": parsed["published_date"],
                        "posted_date": listed_date,
                        "pdf_url": parsed["pdf_url"],
                        "original_filename": parsed["original_filename"],
                        "keywords": parsed["keywords"],
                        "category": parsed["category"],
                        "doi": None,
                        "post_number": parsed["post_number"],
                        "metadata": json.dumps(parsed["metadata"], ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {parsed['title'][:80]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    a_el = item.select_one("a[href]")
                    href_val = a_el["href"] if a_el else "unknown"
                    print(f"[publichealthscotland-scot-publications] item {href_val} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break

            if not any_new:
                print(f"[{self.site_id}] page {page} had no new items; stopping")
                break

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Detail page parser
    # ------------------------------------------------------------------

    def _parse_detail(
        self, soup, url,
        list_title, list_desc,
        listed_date, listed_date_raw, categories,
    ):
        # External ID from URL slug
        path = urlparse(url).path.rstrip("/")
        external_id = path.split("/")[-1] if path else url

        # Title: use the more specific list_title (includes release date)
        h1 = soup.find("h1")
        series_title = self._clean_text(h1) if h1 else ""
        title = list_title or series_title or "(untitled)"

        # Meta description — extracted before any soup mutation
        meta_desc = ""
        meta_tag = soup.find("meta", attrs={"name": "description"})
        if meta_tag and meta_tag.get("content"):
            meta_desc = self._clean_text(meta_tag["content"])

        # Structured metadata from dl (before soup mutation)
        published_date = ""
        type_str = ""
        author_str = ""

        for dl in soup.find_all("dl"):
            for dt in dl.find_all("dt"):
                dd = dt.find_next_sibling("dd")
                dt_text = dt.get_text(strip=True).lower()
                dd_text = dd.get_text(strip=True) if dd else ""
                if "published" in dt_text and not published_date:
                    date_part = re.sub(r'\([^)]*\)', '', dd_text).strip()
                    published_date = self._parse_date(date_part)
                elif dt_text == "type" and not type_str:
                    type_str = dd_text
                elif "author" in dt_text and not author_str:
                    author_str = dd_text

        if not published_date:
            published_date = listed_date

        # PDF link (before soup mutation)
        pdf_url = None
        original_filename = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if ".pdf" in href.lower():
                pdf_url = urljoin(self.base_url, href)
                fname = href.rstrip("/").split("/")[-1].split("?")[0]
                if "." in fname:
                    original_filename = fname
                break

        # Build abstract: meta desc, then body text fallback, then list desc
        abstract = meta_desc
        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            body_text = self._extract_body_text(soup)
            combined = self._join_limited([abstract, body_text], limit=2000)
            if len(combined) > len(abstract):
                abstract = combined

        if len(abstract) < self.MIN_ABSTRACT_CHARS and list_desc:
            combined = self._join_limited([abstract, list_desc], limit=2000)
            if len(combined) > len(abstract):
                abstract = combined

        # Category and keywords
        category_parts = ([type_str] if type_str else []) + categories
        category = "; ".join(category_parts) if category_parts else None

        kw_list = list(dict.fromkeys(categories))
        if type_str and type_str not in kw_list:
            kw_list.insert(0, type_str)
        keywords = ", ".join(kw_list) if kw_list else None

        publisher = author_str or "Public Health Scotland"
        authors = author_str or "Public Health Scotland"

        metadata = {
            "posted_date": listed_date_raw,
            "type": type_str,
            "series_title": series_title,
            "list_categories": categories,
        }

        return {
            "external_id": external_id,
            "post_number": external_id,
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "publisher": publisher,
            "published_date": published_date,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "keywords": keywords,
            "category": category,
            "metadata": metadata,
        }

    def _extract_body_text(self, soup):
        """Extract meaningful paragraphs from the detail page body."""
        main = soup.find("main") or soup.find("body")
        if not main:
            return ""

        for tag in main.find_all(
            ["script", "style", "noscript", "nav", "footer", "header", "button", "form"]
        ):
            tag.decompose()

        _SKIP = (
            "skip to", "cookie", "share this", "sign up", "subscribe",
            "print this", "feedback", "contact us", "date modified",
            "open in new", "opens in new", "back to top",
        )

        parts = []
        for p in main.select("p"):
            text = self._clean_text(p)
            if len(text) < 30:
                continue
            if any(ph in text.lower() for ph in _SKIP):
                continue
            parts.append(text)
            if len(" ".join(parts)) >= 1500:
                break

        return self._join_limited(parts, limit=1800)

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, context="request"):
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--max-time", "45", "--connect-timeout", "15",
            "-A", self.USER_AGENT,
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: en-GB,en;q=0.9",
            url,
        ]

        last_error = ""
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=55)
                stdout = result.stdout or b""
                if result.returncode == 0 and stdout.strip():
                    return stdout.decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                last_error = stderr or f"curl exit {result.returncode}"
            except subprocess.TimeoutExpired:
                last_error = "curl timeout"
            except Exception as exc:
                last_error = str(exc)

            wait = self.BACKOFF[min(attempt, len(self.BACKOFF) - 1)]
            print(
                f"[{self.site_id}] {context} curl failed "
                f"(attempt {attempt + 1}/3): {last_error}"
            )
            if attempt < 2:
                print(f"[{self.site_id}] retrying in {wait}s...")
                time.sleep(wait)

        print(f"[{self.site_id}] {context} failed after 3 attempts: {last_error}")
        return None

    def _make_soup(self, raw):
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                last_exc = exc
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed: {exc}")
        print(f"[{self.site_id}] all parsers failed: {last_exc}")
        return None

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

    def _parse_date(self, raw):
        text = self._clean_text(raw)
        if not text:
            return ""
        m = re.search(r'(\d{4})-(\d{2})-(\d{2})', text)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        m = re.search(r'(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})', text)
        if m:
            day, month_name, year = m.group(1), m.group(2), m.group(3)
            month_num = self._MONTHS.get(month_name[:3].lower())
            if month_num:
                return f"{year}-{month_num:02d}-{int(day):02d}"
        return ""

    def _clean_text(self, value):
        if value is None:
            return ""
        if hasattr(value, "get_text"):
            value = value.get_text(" ", strip=True)
        text = unescape(str(value)).replace("\xa0", " ").replace("​", "").replace("﻿", "")
        return re.sub(r"\s+", " ", text).strip()

    def _join_limited(self, parts, limit=1800):
        text = re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()
        if len(text) <= limit:
            return text
        cut = text[:limit].rsplit(" ", 1)[0].rstrip(" .,;:")
        return f"{cut}."
