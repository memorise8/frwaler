# -*- coding: utf-8 -*-
"""Crawler for digar.ee — Eesti Rahvusraamatukogu (National Library of
Estonia) digital archive, DIGAR.

DIGAR has no public JSON/REST API. List pages are server-rendered HTML at
``/arhiiv/et/raamatud?id=<category>&sort=2&page=<n>`` (``sort=2`` = newest
first). Each list item links to a detail page
(``/arhiiv/et/raamatud/<numeric_id>``) carrying the full bibliographic
metadata (author, publisher, year, language, ISBN, keywords, series).

DIGAR catalog entries have no free-text abstract — this is a digitized
book archive, not a paper repository — so the ``abstract`` field is
synthesized from the detail-page metadata (title, authors, publisher,
year, category, keywords, ISBN, permalink).
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


_SITE_ID = "digar-ee"

# Top-level "Raamatud" (Books) categories, discovered from /arhiiv/et/raamatud.
_CATEGORIES = [
    ("123", "Filoloogia. Ilukirjandus"),
    ("24", "Filosoofia"),
    ("131", "Geograafia. Ajalugu"),
    ("111", "Kunst. Meelelahutused. Sport"),
    ("64", "Loodus- ja täppisteadused"),
    ("72", "Rakendusteadused. Arstiteadus. Tehnika"),
    ("29", "Religioon. Teoloogia"),
    ("9", "Teaduse ja kultuuri üldküsimused"),
    ("33", "Ühiskonnateadused"),
]


class DigarEeCrawler(BaseCrawler):
    site_id = "digar-ee"
    site_name = "Custom: digar-ee"
    base_url = "https://www.digar.ee"

    LIST_PATH = "/arhiiv/et/raamatud"
    MAX_PAGES = 200
    WALL_BUDGET_SECONDS = 25 * 60
    CURL_TIMEOUT = 45
    BACKOFF_SECONDS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 50
    CURL_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
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
        page_counter = 0
        start_time = time.monotonic()
        limit_or_inf = str(limit) if limit is not None else "inf"

        print(f"[{_SITE_ID}] starting crawl (limit={limit_or_inf})")
        print(f"[{_SITE_ID}] list endpoint: {self.base_url}{self.LIST_PATH}?id=<cat>&sort=2&page=<n>")
        print(f"[{_SITE_ID}] detail endpoint: {self.base_url}{self.LIST_PATH}/<id>")

        stop_all = False
        for cat_id, cat_name in _CATEGORIES:
            if stop_all:
                break
            if limit is not None and saved >= limit:
                break

            page = 0
            while True:
                page += 1
                page_counter += 1

                elapsed = time.monotonic() - start_time
                if elapsed > self.WALL_BUDGET_SECONDS:
                    print(f"[{_SITE_ID}] 25-minute budget reached at global page {page_counter}; stopping")
                    stop_all = True
                    break

                if page_counter > self.MAX_PAGES:
                    print(f"[{_SITE_ID}] safety cap of {self.MAX_PAGES} pages reached; stopping")
                    stop_all = True
                    break

                if page_counter % 10 == 0 or page_counter == 1:
                    print(f"[{_SITE_ID}] page {page_counter}: saved {saved}/{limit_or_inf}")

                if limit is not None and saved >= limit:
                    break

                list_url = f"{self.base_url}{self.LIST_PATH}?id={cat_id}&sort=2&page={page}"
                raw = self._curl_text(list_url, context=f"{cat_name} page {page}", referer=self.base_url)
                if not raw:
                    print(f"[{_SITE_ID}] {cat_name} page {page}: empty list response; moving to next category")
                    break

                soup = self._make_soup(raw, context=f"{cat_name} page {page}")
                if soup is None:
                    print(f"[{_SITE_ID}] {cat_name} page {page}: HTML parse failed; moving to next category")
                    break

                records = self._parse_list_records(soup, cat_id)
                if not records:
                    print(f"[{_SITE_ID}] {cat_name} page {page}: 0 records; category exhausted")
                    break

                page_had_new = False
                for item_index, record in enumerate(records, start=1):
                    if limit is not None and saved >= limit:
                        break
                    elapsed = time.monotonic() - start_time
                    if elapsed > self.WALL_BUDGET_SECONDS:
                        print(f"[{_SITE_ID}] 25-minute budget reached during {cat_name} page {page}; stopping")
                        stop_all = True
                        break

                    item_url = record.get("item_url") or ""
                    if not item_url or item_url in seen_urls:
                        continue
                    seen_urls.add(item_url)
                    page_had_new = True

                    try:
                        time.sleep(self.detail_delay)
                        paper = self._fetch_and_build_paper(record, cat_name, list_url)
                        if paper is None:
                            continue
                        abstract = paper.get("abstract") or ""
                        if len(abstract) < self.MIN_ABSTRACT_CHARS:
                            print(
                                f"[{_SITE_ID}] item {item_index} skipped: "
                                f"abstract too short ({len(abstract)} chars)"
                            )
                            continue
                        self._save_paper(paper)
                        saved += 1
                        print(f"[{_SITE_ID}] saved {saved}/{limit_or_inf}: {paper['title'][:80]}")
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        label = record.get("title") or item_url or f"item {item_index}"
                        print(f"[{_SITE_ID}] item {label[:80]} failed: {exc}")
                        continue

                if stop_all:
                    break

                if not page_had_new:
                    print(f"[{_SITE_ID}] {cat_name} page {page}: all URLs already seen; category exhausted")
                    break

                if not self._has_next_page(soup):
                    print(f"[{_SITE_ID}] {cat_name} page {page}: next page link absent; category exhausted")
                    break

        print(f"[{_SITE_ID}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Detail fetch + paper building
    # ------------------------------------------------------------------

    def _fetch_and_build_paper(self, record, cat_name, referer):
        item_url = record["item_url"]
        raw = self._curl_text(item_url, context=f"detail {item_url}", referer=referer)
        if not raw:
            raise RuntimeError(f"detail page fetch failed after retries: {item_url}")

        soup = self._make_soup(raw, context=f"detail {item_url}")
        if soup is None:
            raise RuntimeError(f"detail page HTML parse failed: {item_url}")

        numeric_id = record.get("numeric_id")
        if not numeric_id:
            raise ValueError(f"no numeric id resolvable from url: {item_url}")

        obj_div = soup.select_one("div.object[data-digar-id]")
        native_id = obj_div.get("data-digar-id") if obj_div else record.get("viewer_native_id")

        title_el = soup.select_one("h1.object-title")
        title_text = self._clean(title_el.get_text(" ", strip=True)) if title_el else record.get("title")
        if not title_text:
            raise ValueError(f"no title resolvable: {item_url}")

        author_el = soup.select_one("span.object-author")
        author_text = self._clean(author_el.get_text(" ", strip=True)) if author_el else (record.get("list_author") or "")
        authors_joined = None
        if author_text:
            parts = [p.strip() for p in author_text.split(",") if p.strip()]
            authors_joined = "; ".join(parts) if parts else None

        publishers = [self._clean(dd.get_text(strip=True)) for dd in soup.select('dd[itemprop="publisher"]')]
        publishers = [p for p in publishers if p]
        publisher_joined = "; ".join(publishers) if publishers else (record.get("list_publisher") or None)

        year = None
        year_dd = soup.select_one('dd[itemprop="datePublished"]')
        if year_dd is not None:
            year = self._clean(year_dd.get("content") or year_dd.get_text(strip=True)) or None
        if not year:
            year = record.get("list_year")

        language = None
        depositor = None
        for dt in soup.select("dl.meta-list dt"):
            label = self._clean(dt.get_text(strip=True))
            if not label:
                continue
            dd = dt.find_next_sibling("dd")
            if dd is None:
                continue
            if label == "Keel:":
                language = self._clean(dd.get_text(strip=True)) or None
            elif label == "Deposiitor:":
                depositor = self._clean(dd.get_text(strip=True)) or None

        isbn_dd = soup.select_one('dd[itemprop="ISBN"]')
        isbn = self._clean(isbn_dd.get_text(strip=True)) if isbn_dd else None

        ester_a = soup.select_one('dd[itemprop="ESTER"] a')
        ester_id = self._clean(ester_a.get_text(strip=True)) if ester_a else None
        ester_href = urljoin(self.base_url, ester_a["href"]) if (ester_a and ester_a.get("href")) else None

        bookformat_dd = soup.select_one('dd[itemprop="bookFormat"]')
        book_format = self._clean(bookformat_dd.get_text(strip=True)) if bookformat_dd else None

        keywords = [self._clean(a.get_text(strip=True)) for a in soup.select("div.tag-list a.tag-item")]
        keywords = [k for k in keywords if k]

        permalink_a = soup.select_one('p a[href*="/id/nlib-digar"]')
        permalink = permalink_a.get("href") if permalink_a else None
        if not native_id and permalink:
            match = re.search(r"(nlib-digar:[\w-]+)", permalink)
            if match:
                native_id = match.group(1)
        if not native_id:
            native_id = f"raamatud-{numeric_id}"

        series_groups = []
        for group in soup.select("div.object-related-series-group"):
            chain = [self._clean(a.get_text(strip=True)) for a in group.select("a")]
            chain = [c for c in chain if c]
            if chain:
                series_groups.append(" > ".join(chain))

        breadcrumb_texts = [self._clean(a.get_text(strip=True)) for a in soup.select("div.breadcrumbs a")]
        breadcrumb_texts = [b for b in breadcrumb_texts if b]
        subcategory = breadcrumb_texts[-1] if len(breadcrumb_texts) >= 2 else None

        abstract = self._build_abstract(
            title=title_text,
            authors=authors_joined,
            publisher=publisher_joined,
            year=year,
            language=language,
            category=cat_name,
            subcategory=subcategory,
            keywords=keywords,
            isbn=isbn,
            permalink=permalink or item_url,
        )

        metadata = {
            "list_endpoint": f"{self.base_url}{self.LIST_PATH}?id={record.get('cat_id')}&sort=2",
            "detail_endpoint": item_url,
            "posted_date": year,
            "originalFilename": None,
            "journal_raw": None,
            "series": series_groups or None,
            "volume": None,
            "issue": None,
            "native_digar_id": native_id,
            "numeric_id": numeric_id,
            "isbn": isbn,
            "ester_id": ester_id,
            "ester_url": ester_href,
            "language": language,
            "depositor": depositor,
            "book_format": book_format,
            "category_top": cat_name,
            "category_sub": subcategory,
            "keywords_list": keywords,
            "permalink": permalink,
            "viewer_url": record.get("viewer_url"),
        }

        return {
            "id": f"{self.site_id}:{numeric_id}",
            "site_id": self.site_id,
            "external_id": numeric_id,
            "post_number": numeric_id,
            "title": title_text,
            "abstract": abstract,
            "published_date": year,
            "listed_date": year,
            "posted_date": year,
            "authors": authors_joined,
            "publisher": publisher_joined,
            "department": None,
            "journal": None,
            "url": item_url,
            "pdf_url": None,
            "keywords": ", ".join(keywords) if keywords else None,
            "category": cat_name,
            "doi": None,
            "original_filename": None,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_text(self, url, *, context, referer=None):
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
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H",
            "Accept-Language: et,en;q=0.9",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        return self._run_curl(cmd, context=context)

    def _run_curl(self, cmd, *, context):
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
                print(f"[{_SITE_ID}] {context} failed (attempt {attempt}/3): {last_error}; retrying in {wait}s")
                time.sleep(wait)

        print(f"[{_SITE_ID}] {context} failed after 3 attempts: {last_error}")
        return None

    # ------------------------------------------------------------------
    # HTML parsing
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

    def _parse_list_records(self, soup, cat_id):
        records = []
        for li in soup.select("ul.list-items li.list-item"):
            title_a = li.select_one("h2.item-title a[href]")
            if not title_a:
                continue
            item_url = urljoin(self.base_url, title_a["href"])
            title = self._clean(title_a.get_text(" ", strip=True))
            if not title:
                continue

            match = re.search(r"/raamatud/(\d+)", item_url)
            numeric_id = match.group(1) if match else None

            author_div = li.select_one("div.item-author")
            list_author = self._clean(author_div.get_text(" ", strip=True)) if author_div else ""

            list_publisher = None
            list_year = None
            meta_short = li.select_one("div.item-meta-short")
            if meta_short is not None:
                pub_span = meta_short.select_one('span[itemprop="publisher"]')
                if pub_span is not None:
                    list_publisher = self._clean(pub_span.get_text(strip=True)) or None
                date_span = meta_short.select_one('span[itemprop="datePublished"]')
                if date_span is not None:
                    list_year = self._clean(date_span.get_text(strip=True)) or None

            viewer_a = li.select_one('a[href*="/viewer/"]')
            viewer_native_id = None
            viewer_url = None
            if viewer_a is not None and viewer_a.get("href"):
                viewer_url = urljoin(self.base_url, viewer_a["href"])
                match_native = re.search(r"(nlib-digar:[\w-]+)", viewer_a["href"])
                if match_native:
                    viewer_native_id = match_native.group(1)

            records.append({
                "title": title,
                "item_url": item_url,
                "numeric_id": numeric_id,
                "list_author": list_author,
                "list_publisher": list_publisher,
                "list_year": list_year,
                "viewer_native_id": viewer_native_id,
                "viewer_url": viewer_url,
                "cat_id": cat_id,
            })
        return records

    @staticmethod
    def _has_next_page(soup):
        return soup.select_one('a[rel="next"]') is not None

    # ------------------------------------------------------------------
    # Misc helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean(value):
        if value is None:
            return ""
        return re.sub(r"\s+", " ", str(value)).strip()

    @staticmethod
    def _build_abstract(*, title, authors, publisher, year, language, category, subcategory, keywords, isbn, permalink):
        parts = [f'"{title}" on Eesti Rahvusraamatukogu digiarhiivi DIGAR kogusse kuuluv digiteeritud teos.']
        if authors:
            parts.append(f"Autorlus: {authors}.")
        if publisher:
            parts.append(f"Kirjastaja: {publisher}.")
        if year:
            parts.append(f"Ilmumisaasta: {year}.")
        if language:
            parts.append(f"Keel: {language}.")
        cat_label = subcategory or category
        if cat_label:
            parts.append(f"DIGARi kataloogi kategooria: {cat_label}.")
        if keywords:
            parts.append(f"Märksõnad: {', '.join(keywords)}.")
        if isbn:
            parts.append(f"ISBN: {isbn}.")
        if permalink:
            parts.append(f"Kirje püsiviide: {permalink}.")
        return " ".join(parts)
