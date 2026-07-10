# -*- coding: utf-8 -*-
"""Crawler for mindigital.gr Ministry of Digital Governance press releases."""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

# Greek month name → integer
_GREEK_MONTHS = {
    'Ιαν': 1, 'Φεβ': 2, 'Μάρ': 3, 'Μαρ': 3, 'Απρ': 4, 'Μάι': 5,
    'Ιούν': 6, 'Ιούλ': 7, 'Αύγ': 8, 'Αυγ': 8, 'Σεπ': 9,
    'Οκτ': 10, 'Νοέ': 11, 'Νοε': 11, 'Δεκ': 12,
    'Ιανουαρίου': 1, 'Φεβρουαρίου': 2, 'Μαρτίου': 3, 'Απριλίου': 4,
    'Μαΐου': 5, 'Ιουνίου': 6, 'Ιουλίου': 7, 'Αυγούστου': 8,
    'Σεπτεμβρίου': 9, 'Οκτωβρίου': 10, 'Νοεμβρίου': 11, 'Δεκεμβρίου': 12,
}
_GREEK_MONTH_PAT = '|'.join(
    re.escape(k) for k in sorted(_GREEK_MONTHS, key=len, reverse=True)
)


class MindigitalGrArchivesCrawler(BaseCrawler):
    site_id = "mindigital-gr-archives"
    site_name = "Custom: mindigital-gr-archives"
    base_url = "https://www.mindigital.gr"

    START_URL = "https://www.mindigital.gr/archives/category/deltia-typou-anakoinoseis"
    BACKOFF_SECONDS = (1, 3, 9)
    CURL_TIMEOUT = 45
    MIN_ABSTRACT_CHARS = 50
    MIN_SAVED_ABSTRACT_CHARS = 100
    PAGE_CAP = 200
    WALL_CLOCK_LIMIT = 25 * 60  # seconds

    _CURL_META_MARKER = "__MINDIGITAL_GR_CURL_META__:"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls: set[str] = set()
        start_time = time.monotonic()

        while True:
            if time.monotonic() - start_time > self.WALL_CLOCK_LIMIT:
                print(f"[{self.site_id}] wall-clock budget exceeded; stopping cleanly")
                break

            if limit is not None and saved >= limit:
                break

            if page > self.PAGE_CAP:
                print(f"[{self.site_id}] safety page cap ({self.PAGE_CAP}) reached; stopping")
                break

            list_url = self._list_url(page)
            raw = self._curl_get(list_url, context=f"list page {page}",
                                 referer=self.base_url + "/")
            if not raw:
                print(f"[{self.site_id}] list fetch failed at page {page}; stopping")
                break

            soup = self._make_soup(raw, context=f"list page {page}")
            if soup is None:
                print(f"[{self.site_id}] list page {page} could not be parsed; stopping")
                break

            records = self._parse_list(soup, list_url)
            if not records:
                print(f"[{self.site_id}] no records on page {page}; stopping")
                break

            print(f"[{self.site_id}] page {page}: discovered {len(records)} records")

            for idx, record in enumerate(records, start=1):
                if limit is not None and saved >= limit:
                    break

                item_label = f"p{page}.{idx}"
                try:
                    detail_url = record.get("url") or ""
                    if not detail_url:
                        raise RuntimeError("no detail URL in record")
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)

                    time.sleep(self.detail_delay)
                    detail_raw = self._curl_get(
                        detail_url,
                        context=f"item {item_label} detail",
                        referer=list_url,
                    )
                    if not detail_raw:
                        raise RuntimeError("detail fetch failed after retries")

                    detail_soup = self._make_soup(
                        detail_raw, context=f"item {item_label} detail"
                    )
                    if detail_soup is None:
                        raise RuntimeError("detail page could not be parsed")

                    parsed = self._parse_detail(detail_soup, record)
                    abstract = parsed.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue
                    if len(abstract) < self.MIN_SAVED_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] {item_label} skipped: "
                            f"abstract below save threshold ({len(abstract)} chars)"
                        )
                        continue

                    paper = {
                        "site_id": self.site_id,
                        "external_id": parsed["external_id"],
                        "post_number": parsed["post_number"],
                        "title": parsed["title"],
                        "abstract": abstract,
                        "published_date": parsed.get("published_date"),
                        "listed_date": parsed.get("listed_date"),
                        "url": parsed["url"],
                        "pdf_url": parsed.get("pdf_url"),
                        "category": parsed.get("category"),
                        "publisher": parsed.get("publisher"),
                        "authors": parsed.get("authors"),
                        "keywords": parsed.get("keywords"),
                        "original_filename": parsed.get("original_filename"),
                        "metadata": json.dumps(
                            parsed.get("metadata", {}), ensure_ascii=False
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    limit_str = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(
                        f"[{self.site_id}] saved {limit_str}: "
                        f"{parsed['title'][:90]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if page % 10 == 0:
                limit_or_inf = limit if limit is not None else "∞"
                print(
                    f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}"
                )

            if limit is not None and saved >= limit:
                break

            if not self._has_next_page(soup, page):
                break

            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # URL / pagination helpers
    # ------------------------------------------------------------------

    def _list_url(self, page):
        if page <= 1:
            return self.START_URL
        return f"{self.START_URL}/page/{page}"

    def _has_next_page(self, soup, page):
        if soup.select_one("a.next.page-numbers"):
            return True
        if soup.select_one(f'a.page-numbers[href*="/page/{page + 1}"]'):
            return True
        # Fall back to <link rel="next"> in <head>
        if soup.find("link", rel=lambda r: r and "next" in r):
            return True
        return False

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, context="request", referer=None, accept=None):
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--connect-timeout", "15",
            "--max-time", str(self.CURL_TIMEOUT),
            "-A", self.USER_AGENT,
            "-H",
            (
                "Accept: "
                + (
                    accept
                    or "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,*/*;q=0.8"
                )
            ),
            "-H", "Accept-Language: el-GR,el;q=0.9,en;q=0.7",
            "-H", "Cache-Control: no-cache",
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
                    cmd, capture_output=True, timeout=self.CURL_TIMEOUT + 10
                )
                stdout = (result.stdout or b"").decode("utf-8", errors="replace")
                body, http_code, effective_url = self._split_curl_output(stdout, url)

                if result.returncode != 0:
                    stderr = (
                        (result.stderr or b"")
                        .decode("utf-8", errors="replace")
                        .strip()
                    )
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if http_code and int(http_code) >= 400:
                    raise RuntimeError(f"HTTP {http_code} for {effective_url}")
                if not body.strip():
                    raise RuntimeError("empty response")
                return body

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
            f"[{self.site_id}] {context} failed after 3 attempts: {last_error}"
        )
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
    # HTML parsing
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
                print(
                    f"[{self.site_id}] BeautifulSoup({parser}) failed "
                    f"for {context}: {exc}"
                )
        print(
            f"[{self.site_id}] all parsers failed for {context}: {last_exc}"
        )
        return None

    def _parse_list(self, soup, list_url):
        records = []
        seen: set[str] = set()

        for article in soup.find_all("article", class_="type-post"):
            link_node = (
                article.select_one("h2.entry-title a[href]")
                or article.select_one("h1.entry-title a[href]")
                or article.select_one("h3.entry-title a[href]")
                or article.select_one(".entry-title a[href]")
            )
            if link_node is None:
                continue

            url = urljoin(self.base_url, link_node.get("href", "").strip())
            if not url or url in seen:
                continue
            seen.add(url)

            title = self._node_text(link_node)
            if not title:
                continue

            time_node = article.find("time")
            datetime_attr = time_node.get("datetime", "") if time_node else ""
            date_text_raw = self._node_text(time_node) if time_node else ""

            cat_node = article.select_one(".cat-links a")
            category = (
                self._node_text(cat_node)
                if cat_node
                else "Δελτία Τύπου - Ανακοινώσεις"
            )

            excerpt_node = article.select_one(".entry-content p")
            teaser = self._node_text(excerpt_node) if excerpt_node else ""

            post_number = self._post_number_from_url(url)
            listed_date = self._parse_date(datetime_attr) or self._parse_greek_date(
                date_text_raw
            )

            records.append(
                {
                    "title": title,
                    "url": url,
                    "external_id": post_number or self._slug_from_url(url),
                    "post_number": post_number,
                    "listed_date": listed_date,
                    "category": category,
                    "teaser": teaser,
                    "list_url": list_url,
                    "list_date_text_raw": date_text_raw,
                }
            )

        return records

    def _parse_detail(self, soup, record):
        url = record.get("url", "")

        # --- JSON-LD (date, title fallback) ---
        jsonld_article = self._find_jsonld_article(soup)

        # --- Title ---
        title = (
            self._clean_title(
                self._node_text(soup.select_one("h1.elementor-heading-title"))
            )
            or self._clean_title(
                self._node_text(
                    soup.select_one(
                        '[data-widget_type="theme-post-title.default"] h1'
                    )
                )
            )
            or self._clean_title(
                self._node_text(soup.select_one("h1"))
            )
            or self._clean_title(jsonld_article.get("headline", ""))
            or record.get("title", "")
        )
        title = self._clean_title(title)
        if not title:
            raise RuntimeError("no title found on detail page")

        # --- Date ---
        # Primary: JSON-LD datePublished
        published_date = self._parse_date(
            jsonld_article.get("datePublished", "")
        )
        # Fallback: first .elementor-post-date (belongs to the current article)
        if not published_date:
            date_node = soup.select_one(
                '[data-widget_type="theme-post-info.default"] .elementor-post-date'
            ) or soup.select_one(".elementor-post-info .elementor-post-date")
            if date_node:
                published_date = self._parse_greek_date(
                    self._node_text(date_node)
                )
        date_text_raw = (
            self._node_text(soup.select_one(".elementor-post-date"))
            or record.get("list_date_text_raw", "")
        )
        listed_date = record.get("listed_date") or published_date

        # --- Category ---
        info_node = soup.select_one(
            '[data-widget_type="theme-post-info.default"]'
        )
        cat_link = info_node.select_one("a") if info_node else None
        category = (
            self._node_text(cat_link)
            if cat_link
            else record.get("category", "Δελτία Τύπου - Ανακοινώσεις")
        )

        # --- Content / Abstract ---
        content_node = soup.select_one(
            ".elementor-widget-theme-post-content"
        ) or soup.select_one(
            '[data-widget_type="theme-post-content.default"]'
        )

        abstract = ""
        pdf_url = None
        original_filename = None
        attachment_links: list[dict] = []

        if content_node:
            # Collect PDF/attachment links before stripping HTML
            for a in content_node.find_all("a", href=True):
                href = urljoin(self.base_url, a.get("href", ""))
                label = self._node_text(a)
                if self._looks_like_pdf(href):
                    attachment_links.append({"label": label, "url": href})
                elif self._looks_like_attachment(href):
                    attachment_links.append({"label": label, "url": href})
            if attachment_links:
                pdf_url = attachment_links[0]["url"]
                original_filename = self._filename_from_url(pdf_url)

            # Extract text
            frag = self._make_soup(str(content_node), context="content fragment")
            if frag:
                for bad in frag.select(
                    "script, style, noscript, .elementor-share-btn, "
                    ".wp-caption-text, figure"
                ):
                    bad.decompose()
                parts = []
                for node in frag.find_all(
                    ["p", "li", "h2", "h3", "h4"], recursive=True
                ):
                    txt = self._node_text(node)
                    if txt and txt not in parts:
                        parts.append(txt)
                if parts:
                    abstract = self._clean_multiline("\n\n".join(parts))
                else:
                    abstract = self._clean_multiline(
                        frag.get_text("\n", strip=True)
                    )

        # Last-resort: use teaser from list page
        if not abstract:
            abstract = record.get("teaser", "")

        # --- Identifiers ---
        post_number = record.get("post_number") or self._post_number_from_url(url)
        external_id = (
            post_number
            or record.get("external_id")
            or self._slug_from_url(url)
        )

        # --- Metadata ---
        metadata = {
            "source": "mindigital.gr WordPress/Elementor HTML",
            "list_url": record.get("list_url", self.START_URL),
            "posted_date": record.get("listed_date", ""),
            "date_text_raw": date_text_raw,
            "attachment_links": attachment_links,
            "jsonld_article_id": jsonld_article.get("@id", ""),
        }

        return {
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "url": url,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "category": category,
            "publisher": "Υπουργείο Ψηφιακής Διακυβέρνησης",
            "authors": None,
            "keywords": None,
            "metadata": metadata,
        }

    def _find_jsonld_article(self, soup):
        for script in soup.find_all("script", type="application/ld+json"):
            raw = script.string
            if raw is None:
                raw = script.get_text("", strip=False)
            text = str(raw or "").strip()
            if not text:
                continue
            try:
                data = json.loads(text)
            except (TypeError, ValueError):
                cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
                try:
                    data = json.loads(cleaned)
                except (TypeError, ValueError):
                    continue

            items = []
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                graph = data.get("@graph")
                if isinstance(graph, list):
                    items = graph
                else:
                    items = [data]

            for item in items:
                if not isinstance(item, dict):
                    continue
                t = item.get("@type", "")
                types = [t] if isinstance(t, str) else (t if isinstance(t, list) else [])
                if any(x in {"Article", "NewsArticle", "BlogPosting"} for x in types):
                    return item
        return {}

    # ------------------------------------------------------------------
    # Small utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _post_number_from_url(url):
        m = re.search(r"/archives/(\d+)", url or "")
        return m.group(1) if m else None

    @staticmethod
    def _slug_from_url(url):
        path = urlparse(url or "").path.strip("/")
        return path.split("/")[-1] if path else url or ""

    @classmethod
    def _clean_title(cls, value):
        title = cls._one_line(value)
        # Strip site-name suffix sometimes appended by CMS
        title = re.sub(
            r"\s*[|\-]\s*Υπουργείο.*$", "", title, flags=re.IGNORECASE
        )
        return title.strip()

    @staticmethod
    def _clean_text(value):
        text = unescape(str(value or ""))
        text = text.replace("\xa0", " ").replace("​", "")
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        return text.strip()

    @classmethod
    def _one_line(cls, value):
        return re.sub(r"\s+", " ", cls._clean_text(value)).strip()

    @classmethod
    def _clean_multiline(cls, value):
        text = cls._clean_text(value)
        text = re.sub(r" *\n+ *", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def _node_text(cls, node):
        if node is None:
            return ""
        return cls._one_line(node.get_text(" ", strip=True))

    @staticmethod
    def _parse_date(value):
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if not text:
            return ""
        # ISO datetime: 2026-05-28T...  or 2026-05-28
        # NOTE (2026-07-11 fix): trailing \b fails to match when the date
        # is followed by 'T' (digit->letter is not a word boundary), so
        # ISO datetimes like "2026-05-28T..." never matched. Use a
        # not-followed-by-digit lookahead instead.
        m = re.search(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})(?!\d)", text)
        if m:
            return m.group(0)[:10]
        # DD/MM/YYYY
        m = re.search(r"\b(\d{1,2})/(\d{1,2})/((?:19|20)\d{2})\b", text)
        if m:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 1 <= d <= 31 and 1 <= mo <= 12:
                return f"{y:04d}-{mo:02d}-{d:02d}"
        return ""

    @classmethod
    def _parse_greek_date(cls, text):
        """Parse dates like '28 Μαΐου, 2026' → '2026-05-28'."""
        text = cls._one_line(text)
        if not text:
            return ""
        # Pattern: DD <greek_month> [,] YYYY
        m = re.search(
            rf"(\d{{1,2}})\s+({_GREEK_MONTH_PAT})[,\s]+(\d{{4}})",
            text,
        )
        if m:
            day = int(m.group(1))
            month_name = m.group(2)
            year = int(m.group(3))
            mo = _GREEK_MONTHS.get(month_name)
            if mo and 1 <= day <= 31 and 1 <= mo <= 12:
                return f"{year:04d}-{mo:02d}-{day:02d}"
        return ""

    @staticmethod
    def _looks_like_pdf(url):
        return bool(re.search(r"\.pdf(?:[?#]|$)", url or "", flags=re.IGNORECASE))

    @staticmethod
    def _looks_like_attachment(url):
        return bool(
            re.search(
                r"\.(?:docx?|xlsx?|pptx?|zip|rar|odt|ods)(?:[?#]|$)",
                url or "",
                flags=re.IGNORECASE,
            )
        )

    @staticmethod
    def _filename_from_url(url):
        path = urlparse(url or "").path
        if path:
            name = path.rstrip("/").rsplit("/", 1)[-1]
            if name:
                return name
        return None
