# -*- coding: utf-8 -*-
"""Crawler for mintour.gov.gr Δελτία Τύπου.

Starting page:
  https://mintour.gov.gr/deltia-typoy/

The public WordPress REST API returns 401, while the rendered page exposes an
Elementor grid whose /page/2/ URL loops back to the first page. The canonical
category archive is the stable real list endpoint:
  https://mintour.gov.gr/category/deltia-typou/
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


_GREEK_MONTHS = {
    "Ιανουάριος": 1,
    "Ιανουαρίου": 1,
    "Φεβρουάριος": 2,
    "Φεβρουαρίου": 2,
    "Μάρτιος": 3,
    "Μαρτίου": 3,
    "Απρίλιος": 4,
    "Απριλίου": 4,
    "Μάιος": 5,
    "Μαΐου": 5,
    "Μαίου": 5,
    "Ιούνιος": 6,
    "Ιουνίου": 6,
    "Ιούλιος": 7,
    "Ιουλίου": 7,
    "Αύγουστος": 8,
    "Αυγούστου": 8,
    "Σεπτέμβριος": 9,
    "Σεπτεμβρίου": 9,
    "Οκτώβριος": 10,
    "Οκτωβρίου": 10,
    "Νοέμβριος": 11,
    "Νοεμβρίου": 11,
    "Δεκέμβριος": 12,
    "Δεκεμβρίου": 12,
}


class MintourGovGrDeltiaTypoyCrawler(BaseCrawler):
    site_id = "mintour-gov-gr-deltia-typoy"
    site_name = "Custom: mintour-gov-gr-deltia-typoy"
    base_url = "https://mintour.gov.gr"

    START_URL = "https://mintour.gov.gr/deltia-typoy/"
    LIST_URL = "https://mintour.gov.gr/category/deltia-typou/"
    PUBLISHER = "Υπουργείο Τουρισμού"
    CATEGORY = "Δελτία Τύπου"
    BACKOFFS = (1, 3, 9)
    CURL_TIMEOUT = 45
    MAX_PAGES = 200
    WALL_BUDGET_SECONDS = 25 * 60
    MIN_ABSTRACT_CHARS = 50
    CURL_MARKER = "__MINTOUR_CURL_META__:"

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls = set()
        started_at = time.monotonic()
        limit_or_inf = str(limit) if limit is not None else "∞"

        while True:
            if limit is not None and saved >= limit:
                break
            if page > self.MAX_PAGES:
                print(
                    f"[{self.site_id}] safety cap of {self.MAX_PAGES} pages reached; stopping"
                )
                break
            if time.monotonic() - started_at >= self.WALL_BUDGET_SECONDS:
                print(f"[{self.site_id}] 25-minute wall-clock budget reached; stopping")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            list_url = self._list_url(page)
            raw = self._curl_get(list_url, context=f"list page {page}")
            if not raw:
                print(f"[{self.site_id}] list page {page} failed or returned empty; stopping")
                break

            soup = self._make_soup(raw, context=f"list page {page}")
            if soup is None:
                print(f"[{self.site_id}] list page {page} could not be parsed; stopping")
                break

            records = self._parse_list_records(soup, page, list_url)
            if not records:
                print(f"[{self.site_id}] page {page}: no records; done")
                break

            new_on_page = 0
            for idx, record in enumerate(records, start=1):
                if limit is not None and saved >= limit:
                    break

                url = record.get("url")
                if not url:
                    continue
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_on_page += 1

                try:
                    time.sleep(self._delay)
                    if self._process_record(record, label=f"p{page}.i{idx}"):
                        saved += 1
                        print(
                            f"[{self.site_id}] saved {saved}/{limit_or_inf}: "
                            f"{record.get('title', '')[:80]}"
                        )
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {url} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: no new records; stopping")
                break

            if not self._has_next_page(soup, page):
                print(f"[{self.site_id}] no next page after page {page}; done")
                break

            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    def _process_record(self, record, label):
        detail_raw = self._curl_get(record["url"], context=f"detail {label}")
        if not detail_raw:
            print(f"[{self.site_id}] item {label} skipped: detail fetch failed")
            return 0

        soup = self._make_soup(detail_raw, context=f"detail {label}")
        if soup is None:
            print(f"[{self.site_id}] item {label} skipped: detail parse failed")
            return 0

        detail = self._parse_detail(soup, record)
        abstract = detail["abstract"]
        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            print(
                f"[{self.site_id}] item {label} skipped: abstract "
                f"{len(abstract)} chars < {self.MIN_ABSTRACT_CHARS}"
            )
            return 0

        metadata = {
            "source": "mintour.gov.gr category HTML + detail HTML",
            "start_url": self.START_URL,
            "list_endpoint": self.LIST_URL,
            "list_url": record.get("list_url"),
            "api_probe": "WordPress REST API returned 401; rendered category archive used",
            "node_id": detail["external_id"],
            "wp_post_id": detail["external_id"],
            "post_id": detail["external_id"],
            "slug": detail["slug"],
            "posted_date": record.get("date_raw") or detail.get("date_raw"),
            "listed_date": record.get("listed_date"),
            "published_date_raw": detail.get("date_raw"),
            "list_excerpt": record.get("excerpt"),
            "canonical_url": detail.get("canonical_url"),
            "shortlink": detail.get("shortlink"),
            "originalFilename": detail.get("original_filename"),
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "list_page": record.get("page"),
            "native_classes": record.get("classes"),
            "image_url": record.get("image_url"),
        }

        paper = {
            "id": None,
            "site_id": self.site_id,
            "external_id": detail["external_id"],
            "post_number": detail["post_number"],
            "title": detail["title"],
            "abstract": abstract,
            "published_date": detail["published_date"] or record.get("listed_date"),
            "listed_date": record.get("listed_date"),
            "posted_date": record.get("listed_date"),
            "authors": None,
            "publisher": self.PUBLISHER,
            "department": "Γραφείο Τύπου",
            "journal": None,
            "url": detail.get("canonical_url") or record["url"],
            "pdf_url": detail.get("pdf_url"),
            "keywords": self.CATEGORY,
            "category": self.CATEGORY,
            "doi": None,
            "original_filename": detail.get("original_filename"),
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }
        self._save_paper(paper)
        return 1

    def _list_url(self, page):
        if page <= 1:
            return self.LIST_URL
        return urljoin(self.LIST_URL, f"page/{page}/")

    def _parse_list_records(self, soup, page, list_url):
        records = []
        for article in soup.select("article.elementor-post, article.eael-grid-post"):
            title_link = (
                article.select_one(".elementor-post__title a[href]")
                or article.select_one(".eael-entry-title a[href]")
                or article.select_one("a[rel='bookmark'][href]")
                or article.select_one("a[href]")
            )
            if title_link is None:
                continue

            url = urljoin(self.base_url, title_link.get("href", "").strip())
            if not url.startswith(self.base_url):
                continue

            title = self._one_line(
                title_link.get("title") or title_link.get_text(" ", strip=True)
            )
            if not title:
                continue

            date_raw = self._one_line(
                self._first_text(
                    article,
                    [
                        ".elementor-post-date",
                        ".eael-meta-posted-on time",
                        ".eael-posted-on time",
                    ],
                )
            )
            listed_date = self._parse_greek_date(date_raw)
            excerpt = self._one_line(
                self._first_text(
                    article,
                    [".elementor-post__excerpt", ".eael-grid-post-excerpt p"],
                )
            )
            post_id = self._extract_post_id(article, url)
            image = article.select_one("img[src]")

            records.append(
                {
                    "external_id": post_id,
                    "post_number": post_id,
                    "slug": self._slug_from_url(url),
                    "title": title,
                    "url": url,
                    "date_raw": date_raw,
                    "listed_date": listed_date,
                    "excerpt": excerpt,
                    "page": page,
                    "list_url": list_url,
                    "classes": article.get("class", []),
                    "image_url": urljoin(self.base_url, image.get("src")) if image else None,
                }
            )
        return records

    def _parse_detail(self, soup, record):
        canonical = soup.find("link", rel="canonical")
        canonical_url = canonical.get("href", "").strip() if canonical else ""
        shortlink = soup.find("link", rel="shortlink")
        shortlink_url = shortlink.get("href", "").strip() if shortlink else ""

        title = self._one_line(
            self._first_text(
                soup,
                [
                    "h1.elementor-heading-title",
                    ".elementor-widget-theme-post-title h1",
                    "h1.entry-title",
                    "h1",
                ],
            )
        )
        if not title:
            title = record.get("title") or "(untitled)"

        date_raw = self._one_line(
            self._first_text(
                soup,
                [
                    ".elementor-post-info__item--type-date time",
                    ".elementor-post-info time",
                    ".elementor-post-info__item--type-date",
                    "time",
                ],
            )
        )
        published_date = self._parse_greek_date(date_raw) or record.get("listed_date")

        content = (
            soup.select_one(".elementor-widget-theme-post-content")
            or soup.select_one(".entry-content")
            or soup.select_one("article")
        )
        abstract = self._extract_content_text(content) if content is not None else ""

        pdf_url, original_filename = self._find_pdf(content or soup)
        external_id = (
            self._extract_id_from_shortlink(shortlink_url)
            or record.get("external_id")
            or self._extract_id_from_html(soup)
            or record.get("slug")
        )
        post_number = external_id or record.get("post_number") or record.get("slug")

        return {
            "external_id": str(external_id) if external_id is not None else record.get("slug"),
            "post_number": str(post_number) if post_number is not None else None,
            "slug": record.get("slug") or self._slug_from_url(canonical_url or record["url"]),
            "title": title,
            "abstract": abstract,
            "date_raw": date_raw,
            "published_date": published_date,
            "canonical_url": canonical_url or record["url"],
            "shortlink": shortlink_url or None,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
        }

    def _curl_get(self, url, context="request"):
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
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "-H",
            "Accept-Language: el,en;q=0.8,ko;q=0.5",
            "-w",
            "\n" + self.CURL_MARKER + "%{http_code}\t%{url_effective}",
            url,
        ]
        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.CURL_TIMEOUT + 10,
                )
                stdout = (result.stdout or b"").decode("utf-8", errors="replace")
                body, http_code, _ = self._split_curl_meta(stdout, url)
                if result.returncode != 0:
                    stderr = (result.stderr or b"").decode("utf-8", errors="replace")
                    raise RuntimeError(stderr.strip() or f"curl exit {result.returncode}")
                if http_code:
                    code = int(http_code)
                    if code >= 400:
                        raise RuntimeError(f"HTTP {code}")
                if not body.strip():
                    raise RuntimeError("empty response")
                return body
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                last_error = str(exc)
                print(f"[{self.site_id}] {context} curl attempt {attempt}/3: {last_error}")
                if attempt < 3:
                    wait = self.BACKOFFS[attempt - 1]
                    print(f"[{self.site_id}] retrying in {wait}s...")
                    time.sleep(wait)
        print(f"[{self.site_id}] {context} failed after 3 attempts: {last_error}")
        return None

    def _split_curl_meta(self, raw, fallback_url):
        marker_pos = raw.rfind("\n" + self.CURL_MARKER)
        if marker_pos == -1:
            return raw, "", fallback_url
        body = raw[:marker_pos]
        meta = raw[marker_pos + 1 + len(self.CURL_MARKER) :].strip()
        if "\t" not in meta:
            return body, "", fallback_url
        http_code, effective_url = meta.split("\t", 1)
        return body, http_code.strip(), effective_url.strip() or fallback_url

    def _make_soup(self, raw, context="HTML"):
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        return None

    def _has_next_page(self, soup, page):
        next_page = page + 1
        for a_tag in soup.select("a.page-numbers[href], .pagination a[href], .nav-links a[href]"):
            text = self._one_line(a_tag.get_text(" ", strip=True))
            href = a_tag.get("href", "")
            classes = set(a_tag.get("class", []))
            if "next" in classes:
                return True
            if text == str(next_page) or f"/page/{next_page}/" in href:
                return True
        return False

    def _extract_content_text(self, node):
        for bad in node.select("script, style, noscript, form, nav"):
            bad.decompose()

        parts = []
        for element in node.find_all(["p", "li", "h2", "h3", "h4", "blockquote"]):
            text = self._one_line(element.get_text(" ", strip=True))
            if not text:
                continue
            if text in {"Δελτία Τύπου", "Περισσότερα", "Περισσότερα »"}:
                continue
            if text not in parts:
                parts.append(text)

        if parts:
            return "\n\n".join(parts)
        return self._one_line(node.get_text(" ", strip=True))

    def _find_pdf(self, node):
        if node is None:
            return None, None
        for a_tag in node.find_all("a", href=True):
            href = a_tag.get("href", "").strip()
            if ".pdf" not in href.lower():
                continue
            pdf_url = urljoin(self.base_url, href)
            return pdf_url, self._filename_from_url(pdf_url)
        return None, None

    def _filename_from_url(self, url):
        if not url:
            return None
        path = urlparse(url).path.rstrip("/")
        tail = unquote(path.split("/")[-1])
        if "." in tail and len(tail) <= 240:
            return tail
        return None

    def _extract_post_id(self, article, url):
        data_id = article.get("data-id")
        if data_id and str(data_id).isdigit():
            return str(data_id)
        for class_name in article.get("class", []):
            match = re.fullmatch(r"(?:post|eael-pg-post)-(\d+)", class_name)
            if match:
                return match.group(1)
        return self._slug_from_url(url)

    def _extract_id_from_shortlink(self, url):
        if not url:
            return None
        match = re.search(r"[?&]p=(\d+)", url)
        return match.group(1) if match else None

    def _extract_id_from_html(self, soup):
        for article in soup.find_all("article"):
            for class_name in article.get("class", []):
                match = re.fullmatch(r"post-(\d+)", class_name)
                if match:
                    return match.group(1)
        return None

    def _slug_from_url(self, url):
        path = urlparse(url or "").path.strip("/")
        if not path:
            return None
        return path.split("/")[-1] or None

    def _parse_greek_date(self, text):
        clean = self._one_line(text).replace(",", "")
        if not clean:
            return None
        iso_match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", clean)
        if iso_match:
            return iso_match.group(0)
        numeric = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b", clean)
        if numeric:
            day = int(numeric.group(1))
            month = int(numeric.group(2))
            year = int(numeric.group(3))
            if year < 100:
                year += 2000
            return self._format_date(year, month, day)
        greek = re.search(r"\b(\d{1,2})\s+([Α-ΩΆΈΉΊΌΎΏΪΫα-ωάέήίόύώϊϋΐΰ]+)\s+(\d{4})\b", clean)
        if greek:
            day = int(greek.group(1))
            month_name = greek.group(2)
            month = _GREEK_MONTHS.get(month_name)
            if month is None:
                return None
            year = int(greek.group(3))
            return self._format_date(year, month, day)
        return None

    def _format_date(self, year, month, day):
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return None

    def _first_text(self, node, selectors):
        for selector in selectors:
            try:
                found = node.select_one(selector)
            except Exception:
                found = None
            if found is not None:
                return found.get_text(" ", strip=True)
        return ""

    def _one_line(self, value):
        text = unescape(str(value or "")).replace("\xa0", " ").replace("\u200b", "")
        return re.sub(r"\s+", " ", text).strip()
