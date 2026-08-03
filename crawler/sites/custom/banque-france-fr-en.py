# -*- coding: utf-8 -*-
"""Crawler for Banque de France English annual reports."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import unicodedata
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class BanqueFranceFrEnCrawler(BaseCrawler):
    site_id = "banque-france-fr-en"
    site_name = "Custom: banque-france-fr-en"
    base_url = "https://www.banque-france.fr"

    START_URL = (
        "https://www.banque-france.fr/en/publications-and-research/"
        "our-main-publications/annual-reports"
    )
    BACKOFF_SECONDS = (1, 3, 9)
    CURL_TIMEOUT = 45
    MIN_ABSTRACT_CHARS = 50
    MAX_PAGES = 200
    WALL_CLOCK_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

    _CURL_META_MARKER = "__BANQUE_FRANCE_FR_EN_CURL_META__:"

    MONTHS = {
        "january": 1, "janvier": 1,
        "february": 2, "fevrier": 2,
        "march": 3, "mars": 3,
        "april": 4, "avril": 4,
        "may": 5, "mai": 5,
        "june": 6, "juin": 6,
        "july": 7, "juillet": 7,
        "august": 8, "aout": 8,
        "september": 9, "septembre": 9,
        "october": 10, "octobre": 10,
        "november": 11, "novembre": 11,
        "december": 12, "decembre": 12,
    }

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    def crawl(self, limit=None):
        """Crawl the annual reports listing and each publication detail page."""
        saved = 0
        page = 0
        seen_urls = set()
        item_number = 0
        start_time = time.monotonic()
        limit_str = str(limit) if limit is not None else "∞"

        while True:
            # Wall-clock safety budget
            elapsed = time.monotonic() - start_time
            if elapsed > self.WALL_CLOCK_BUDGET_SECONDS:
                print(
                    f"[{self.site_id}] wall-clock budget reached "
                    f"({elapsed:.0f}s); exiting cleanly"
                )
                break

            if limit is not None and saved >= limit:
                break
            if page >= self.MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {self.MAX_PAGES} pages reached; stopping")
                break

            list_url = self._list_url(page)
            raw = self._curl_get(list_url, context=f"list page {page}", referer=self.base_url + "/en/")
            if not raw:
                print(f"[{self.site_id}] list endpoint failed at page {page}; stopping")
                break

            soup = self._make_soup(raw, context=f"list page {page}")
            if soup is None:
                print(f"[{self.site_id}] list page {page} could not be parsed; stopping")
                break

            records = self._parse_list(soup, list_url)
            if not records:
                print(f"[{self.site_id}] no records found at page {page}; stopping")
                break

            # Deduplicate against already-seen URLs
            new_records = []
            for rec in records:
                if rec["url"] not in seen_urls:
                    seen_urls.add(rec["url"])
                    new_records.append(rec)

            if not new_records:
                print(f"[{self.site_id}] page {page}: all {len(records)} records already seen; stopping")
                break

            if page % 10 == 0:
                print(
                    f"[{self.site_id}] page {page}: saved {saved}/{limit_str}"
                )

            for record in new_records:
                if limit is not None and saved >= limit:
                    break

                elapsed = time.monotonic() - start_time
                if elapsed > self.WALL_CLOCK_BUDGET_SECONDS:
                    print(f"[{self.site_id}] wall-clock budget reached; stopping")
                    break

                item_number += 1
                try:
                    detail_url = record.get("url") or ""
                    if not detail_url:
                        raise RuntimeError("record has no detail URL")

                    time.sleep(self.detail_delay)
                    detail_raw = self._curl_get(
                        detail_url,
                        context=f"item {item_number} detail",
                        referer=list_url,
                    )
                    if not detail_raw:
                        raise RuntimeError("detail fetch failed after retries")

                    detail_soup = self._make_soup(detail_raw, context=f"item {item_number} detail")
                    if detail_soup is None:
                        raise RuntimeError("detail HTML could not be parsed")

                    parsed = self._parse_detail(detail_soup, detail_raw, record)
                    abstract = parsed.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_number} skipped: "
                            f"abstract too short ({len(abstract)} chars): {parsed['title'][:60]}"
                        )
                        continue

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": parsed["external_id"],
                        "title": parsed["title"],
                        "authors": "",
                        "abstract": abstract,
                        "category": parsed["category"],
                        "keywords": parsed["keywords"],
                        "published_date": parsed["published_date"],
                        "url": parsed["url"],
                        "pdf_url": parsed["pdf_url"],
                        "doi": "",
                        "department": parsed["publisher"],
                        "metadata": json.dumps(parsed["metadata"], ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[{self.site_id}] saved {saved}/{limit_str}: "
                        f"{parsed['title'][:80]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[banque-france-fr-en] item {item_number} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break
            if not self._has_next_page(soup, page):
                break
            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, context="request", referer=None, accept=None):
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--connect-timeout", "15",
            "--max-time", str(self.CURL_TIMEOUT),
            "-A", self.USER_AGENT,
            "-H", (
                "Accept: " + (
                    accept or
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "application/json,*/*;q=0.8"
                )
            ),
            "-H", "Accept-Language: en-US,en;q=0.9,fr;q=0.7",
            "-w", "\n" + self._CURL_META_MARKER + "%{http_code}\t%{url_effective}",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=self.CURL_TIMEOUT + 10)
                stdout = (result.stdout or b"").decode("utf-8", errors="replace")
                body, http_code, _ = self._split_curl_output(stdout, url)

                if result.returncode != 0:
                    stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if http_code:
                    try:
                        status = int(http_code)
                    except ValueError:
                        status = 0
                    if status >= 400:
                        raise RuntimeError(f"HTTP {http_code} for {url}")
                if not body.strip():
                    raise RuntimeError(f"empty response for {url}")
                return body

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                last_error = str(exc)
                print(f"[{self.site_id}] {context} curl failed (attempt {attempt}/3): {last_error}")
                if attempt < 3:
                    wait = self.BACKOFF_SECONDS[attempt - 1]
                    print(f"[{self.site_id}] retrying in {wait}s...")
                    time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None

    def _split_curl_output(self, raw, fallback_url):
        marker_pos = raw.rfind("\n" + self._CURL_META_MARKER)
        if marker_pos == -1:
            return raw, "", fallback_url
        body = raw[:marker_pos]
        meta = raw[marker_pos + 1 + len(self._CURL_META_MARKER):].strip()
        if "\t" not in meta:
            return body, "", fallback_url
        http_code, effective_url = meta.split("\t", 1)
        return body, http_code.strip(), effective_url.strip() or fallback_url

    # ------------------------------------------------------------------
    # HTML parsing helpers
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

    def _parse_list(self, soup, list_url):
        """Extract publication records from one list page."""
        records = []
        seen = set()

        # Each publication is an <a class="card card-vertical ..."> linking to the detail page.
        for link in soup.select('a.card-vertical[href], a.card[href]'):
            href = (link.get("href") or "").strip()
            if not href:
                continue
            url = urljoin(self.base_url, href)
            if not self._is_detail_url(url) or url in seen:
                continue
            seen.add(url)

            title_tag = link.select_one(".card-title, h3, h2")
            title = self._clean_text(title_tag) if title_tag else self._clean_text(link)
            if not title:
                continue

            records.append({
                "title": title,
                "url": url,
                "external_id": self._external_id_from_url(url),
                "list_url": list_url,
            })

        return records

    def _parse_detail(self, soup, raw, record):
        """Extract full metadata from a publication detail page."""
        settings = self._drupal_settings(soup)
        path_info = settings.get("path") or {}
        piano = settings.get("piano_props") or {}
        if not isinstance(piano, dict):
            piano = {}

        # Canonical URL
        canonical_tag = soup.find("link", rel="canonical")
        canonical = self._clean_text((canonical_tag or {}).get("href") or "")
        url = canonical or record.get("url") or ""

        # Title: prefer og:title or h1, fall back to list title
        og_title = self._meta_content(soup, "meta", "og:title", "content")
        h1 = soup.find("h1")
        title = (
            self._strip_title_suffix(og_title)
            or self._clean_text(h1)
            or self._clean_text(piano.get("content_title") or "")
            or record.get("title")
            or ""
        )

        # Abstract: prefer the publication-specific header-text field, then meta description
        header_div = soup.select_one(
            ".field--name-field-espaces2-header-text, "
            ".publication-statistique-container .field--type-text-long"
        )
        header_text = self._clean_text(header_div) if header_div else ""

        meta_desc = self._meta_content(soup, "meta", "description", "content")

        # Also try content_description from piano_props (HTML-encoded)
        piano_desc = self._clean_text(
            unescape(piano.get("content_description") or "")
        )

        abstract_parts = []
        for part in [header_text, piano_desc, meta_desc]:
            part = part.strip()
            if part and part not in abstract_parts:
                abstract_parts.append(part)
        abstract = "\n\n".join(abstract_parts).strip()

        # Published date: from custom_publication_statistic_date (DD/MM/YYYY)
        stat_date = piano.get("custom_publication_statistic_date") or ""
        published_date = self._parse_date(stat_date)
        if not published_date:
            # Fallback: search "Published on" or "Updated on" text in page
            published_text = self._find_text(soup, r"\bPublished on\b")
            updated_text = self._find_text(soup, r"\bUpdated on\b")
            published_date = self._parse_date(published_text) or self._parse_date(updated_text)

        # Node ID from currentPath "node/XXXXX"
        current_path = path_info.get("currentPath") or ""
        node_id = ""
        node_match = re.search(r"node/(\d+)", current_path)
        if node_match:
            node_id = node_match.group(1)

        external_id = node_id or record.get("external_id") or self._external_id_from_url(url)

        # PDF URL: first card-download link, then any pdf link
        pdf_url = ""
        original_filename = None
        pdf_node = (
            soup.select_one('a.card-download[href]')
            or soup.select_one('a[data-file-extension="pdf"][href]')
            or soup.select_one('a[href*=".pdf"][href]')
        )
        if pdf_node is not None:
            pdf_href = (pdf_node.get("href") or "").strip()
            if pdf_href:
                pdf_url = urljoin(self.base_url, pdf_href)
                # Original filename: last path segment of PDF URL
                pdf_path = urlparse(pdf_url).path
                original_filename = pdf_path.rsplit("/", 1)[-1] or None

        # Category
        category = (
            self._clean_text(piano.get("Page_categorie") or "")
            or self._clean_text(piano.get("category") or "")
        )

        # Keywords from thematic tags
        keywords_list = []
        page_theme = piano.get("Page_thematique") or ""
        for token in re.split(r"[;,]", page_theme):
            token = self._clean_text(token)
            if token:
                keywords_list.append(token)
        for tag in soup.select("a.thematic-pill span, .field--name-field-espaces2-thematic .field__item"):
            tag_text = self._clean_text(tag)
            if tag_text and tag_text not in keywords_list:
                keywords_list.append(tag_text)
        keywords = ",".join(keywords_list)

        metadata = {
            "node_id": node_id,
            "canonical_url": url,
            "list_url": record.get("list_url") or "",
            "list_title": record.get("title") or "",
            "posted_date": published_date,
            "originalFilename": original_filename,
            "page_category": piano.get("Page_categorie") or "",
            "page_theme": page_theme,
            "page_format": piano.get("Page_publication_format") or "",
            "page_sub_format": piano.get("Page_publication_sous_format") or "",
            "stat_date_raw": stat_date,
        }

        return {
            "external_id": external_id,
            "title": title,
            "abstract": abstract,
            "category": category,
            "keywords": keywords,
            "published_date": published_date,
            "listed_date": published_date,
            "url": url,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "publisher": "Banque de France",
            "metadata": metadata,
        }

    def _has_next_page(self, soup, page):
        next_link = soup.select_one(".pager__item--next a[href]")
        if next_link is not None:
            href = next_link.get("href") or ""
            if "page=" in href:
                return True
        for link in soup.select(".pager a[href]"):
            href = link.get("href") or ""
            match = re.search(r"(?:\?|&)page=(\d+)", href)
            if match and int(match.group(1)) > page:
                return True
        return False

    def _list_url(self, page):
        return f"{self.START_URL}?page={page}"

    def _is_detail_url(self, url):
        parsed = urlparse(url)
        if not parsed.netloc.endswith("banque-france.fr"):
            return False
        # Accept both English and bilingual URL paths for publications
        path = parsed.path
        if "/publications/" in path and path != "/en/publications-and-statistics/publications/":
            return True
        return False

    def _external_id_from_url(self, url):
        path = urlparse(url).path.rstrip("/")
        slug = path.rsplit("/", 1)[-1]
        return slug or url

    def _meta_content(self, soup, tag, name, attr):
        if tag == "link":
            node = soup.find("link", rel=name)
        elif name.startswith("og:"):
            node = soup.find("meta", property=name)
        else:
            node = soup.find("meta", attrs={"name": name})
        if node is None:
            return ""
        return self._clean_text(node.get(attr) or "")

    def _drupal_settings(self, soup):
        node = soup.find("script", attrs={"data-drupal-selector": "drupal-settings-json"})
        if node is None:
            return {}
        raw = node.string or node.get_text() or ""
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _find_text(self, root, pattern):
        regex = re.compile(pattern, re.IGNORECASE)
        for text_node in root.find_all(string=regex):
            parent = getattr(text_node, "parent", None)
            parent_text = self._clean_text(parent) if parent is not None else ""
            return parent_text or self._clean_text(text_node)
        return ""

    def _parse_date(self, raw):
        """Parse various date formats to ISO YYYY-MM-DD."""
        text = self._clean_text(raw)
        if not text:
            return ""

        # ISO format: YYYY-MM-DD or YYYY/MM/DD
        iso = re.search(r"\b(20\d{2}|19\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", text)
        if iso:
            y, m, d = iso.groups()
            return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"

        # European format: DD/MM/YYYY or DD-MM-YYYY (Banque de France uses this)
        euro = re.search(r"\b(\d{1,2})[-/](\d{1,2})[-/](20\d{2}|19\d{2})\b", text)
        if euro:
            d, m, y = euro.groups()
            d_i, m_i, y_i = int(d), int(m), int(y)
            # Sanity check to avoid MM/DD confusion
            if 1 <= m_i <= 12 and 1 <= d_i <= 31:
                return f"{y_i:04d}-{m_i:02d}-{d_i:02d}"

        # "22nd of July 2025" or "July 22, 2025"
        text_norm = self._strip_accents(text).lower()
        match = re.search(
            r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]+)\s+(20\d{2}|19\d{2})\b",
            text_norm,
        )
        if not match:
            match = re.search(
                r"\b([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2}|19\d{2})\b",
                text_norm,
            )
            if match:
                month_name, day, year = match.groups()
                month = self.MONTHS.get(month_name)
                if month:
                    return f"{int(year):04d}-{month:02d}-{int(day):02d}"
        elif match:
            day, month_name, year = match.groups()
            month = self.MONTHS.get(month_name)
            if month:
                return f"{int(year):04d}-{month:02d}-{int(day):02d}"

        return ""

    def _strip_title_suffix(self, title):
        text = self._clean_text(title)
        return re.sub(r"\s*\|\s*Banque de France\s*$", "", text).strip()

    def _strip_accents(self, text):
        normalized = unicodedata.normalize("NFKD", text or "")
        return "".join(ch for ch in normalized if not unicodedata.combining(ch))

    def _clean_text(self, value):
        if value is None:
            return ""
        if hasattr(value, "get_text"):
            text = value.get_text(" ", strip=True)
        else:
            text = str(value)
        text = unescape(text)
        text = text.replace("\xa0", " ").replace("​", "")
        text = unicodedata.normalize("NFKC", text)
        return re.sub(r"\s+", " ", text).strip()
