# -*- coding: utf-8 -*-
"""Telemarksforsking scientific publications crawler.

Discovery notes:
  - The public WordPress REST collection exists at
    ``/wp-json/wp/v2/publication``. Its ``content`` and ``excerpt`` fields are
    empty for these records, so it is useful mainly as native-ID metadata.
  - The requested scientific tab is a server-rendered Elementor loop. The
    stable list endpoint is the page HTML with ``e-page-9534a6f={page}``.
  - Detail pages expose the usable ACF/Elementor fields and abstract in the
    rendered HTML, plus a JSON link to the matching REST record.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from html import unescape
from urllib.parse import parse_qs, unquote, urljoin, urlparse, urlunparse

# Absolute import - spec_from_file_location has no package context.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from crawler.base_crawler import BaseCrawler


_START_URL = "https://telemarksforsking.no/publikasjoner/tab/?tab=vitenskapelig"
_LIST_WIDGET_ID = "9534a6f"
_REST_COLLECTION = "https://telemarksforsking.no/wp-json/wp/v2/publication"
_PAGE_CAP = 200
_CRAWL_BUDGET_SECONDS = 25 * 60
_RETRY_WAITS = (1, 3, 9)

_MONTHS = {
    "jan": 1,
    "january": 1,
    "januar": 1,
    "feb": 2,
    "february": 2,
    "februar": 2,
    "mar": 3,
    "march": 3,
    "mars": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "mai": 5,
    "jun": 6,
    "june": 6,
    "juni": 6,
    "jul": 7,
    "july": 7,
    "juli": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "okt": 10,
    "oktober": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
    "des": 12,
    "desember": 12,
}

_LABELS = {
    "released",
    "utgitt",
    "isbn",
    "publication",
    "type",
    "research group",
    "research groups",
    "forskergruppe",
    "forskergrupper",
    "author",
    "authors",
    "forfatter",
    "forfattere",
    "publisher",
    "utgiver",
    "forlag",
    "journal",
    "tidsskrift",
    "series",
    "serie",
    "volume",
    "volum",
    "issue",
    "nummer",
}


def _clean_text(value: object | None) -> str:
    if value is None:
        return ""
    text = unescape(str(value))
    text = text.replace("\xa0", " ").replace("\u200b", "")
    return re.sub(r"\s+", " ", text).strip(" \t\r\n,;")


def _make_soup(raw: str | None):
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup

            return BeautifulSoup(raw or "", parser)
        except Exception as exc:
            print(f"[telemarksforsking-no-publikasjoner] BeautifulSoup parser {parser} failed: {exc}")
            continue
    return None


def _parse_date(raw: str | None) -> str | None:
    value = _clean_text(raw)
    if not value:
        return None

    match = re.search(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", value)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"

    match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})T", value)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"

    match = re.search(r"\b(\d{1,2})\.?\s+([A-Za-z]+)\s+(\d{4})\b", value, flags=re.I)
    if match:
        month = _MONTHS.get(match.group(2).lower())
        if month:
            return f"{match.group(3)}-{month:02d}-{int(match.group(1)):02d}"

    return None


def _label(text: str | None) -> str:
    value = _clean_text(text).lower()
    value = re.sub(r"[:：]+$", "", value)
    return value


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        cleaned = _clean_text(value)
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


def _join_semicolon(values: list[str]) -> str | None:
    out = _dedupe(values)
    return "; ".join(out) if out else None


def _split_authors(raw: str | None) -> str | None:
    value = _clean_text(raw)
    if not value:
        return None
    value = re.sub(r"\s+(?:og|and)\s+", ", ", value, flags=re.I)
    parts = [p for p in re.split(r"\s*[;,]\s*", value) if p.strip()]
    return _join_semicolon(parts)


def _post_number_from_url(url: str | None) -> str | None:
    if not url:
        return None
    segments = [unquote(s) for s in urlparse(url).path.split("/") if s]
    for segment in reversed(segments):
        if re.fullmatch(r"\d+", segment):
            return segment
    for segment in reversed(segments):
        if segment.lower() not in {"en", "publikasjoner", "publication", "publications"}:
            return segment or None
    return None


def _wp_post_id_from_classes(node) -> str | None:
    classes = node.get("class", []) if node else []
    if isinstance(classes, str):
        classes = classes.split()
    for class_name in classes:
        match = re.fullmatch(r"post-(\d+)", class_name)
        if match:
            return match.group(1)
    for class_name in classes:
        match = re.fullmatch(r"e-loop-item-(\d+)", class_name)
        if match:
            return match.group(1)
    return None


def _canonical_seen_url(url: str) -> str:
    parsed = urlparse(url)
    path = re.sub(r"/+", "/", parsed.path).rstrip("/")
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", "", ""))


def _canonical_page_url(soup, fallback: str) -> str:
    if soup:
        link = soup.find("link", rel=lambda v: v and "canonical" in v)
        href = link.get("href") if link else None
        if href:
            return urljoin(fallback, href)
    return fallback


def _extract_doi(text: str | None) -> str | None:
    value = _clean_text(text)
    if not value:
        return None
    match = re.search(r"(10\.\d{4,9}/[^\s\"'<>]+)", value, flags=re.I)
    if match:
        return match.group(1).rstrip(".,);]")
    parsed = urlparse(value)
    if parsed.netloc.lower().endswith("doi.org") and parsed.path:
        return parsed.path.lstrip("/").rstrip(".,);]")
    return None


def _filename_from_pdf_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    tail = unquote(parsed.path.rstrip("/").split("/")[-1])
    if tail.lower().endswith(".pdf"):
        return tail[:240]
    query = parse_qs(parsed.query)
    file_id = (query.get("fil") or query.get("file") or query.get("id") or [None])[0]
    if file_id:
        base = unquote(str(file_id)).strip()
        if base and not base.lower().endswith(".pdf"):
            base = f"{base}.pdf"
        return base[:240] if base else None
    if tail and "." in tail:
        return tail[:240]
    return None


def _filename_from_headers(raw_headers: str | None) -> str | None:
    if not raw_headers:
        return None

    match = re.search(r"filename\*=UTF-8''([^;\r\n]+)", raw_headers, flags=re.I)
    if match:
        return unquote(match.group(1).strip().strip('"'))[:240]

    match = re.search(r'filename="?([^"\r\n;]+)"?', raw_headers, flags=re.I)
    if match:
        return unquote(match.group(1).strip())[:240]

    locations = re.findall(r"(?im)^location:\s*(\S+)\s*$", raw_headers)
    for location in reversed(locations):
        tail = unquote(urlparse(location).path.rstrip("/").split("/")[-1])
        if tail.lower().endswith(".pdf"):
            return tail[:240]
    return None


def _looks_like_pdf_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    path = parsed.path.lower()
    if path.endswith(".pdf"):
        return True
    if path.endswith("/filer/fil.asp") and parse_qs(parsed.query).get("fil"):
        return True
    return False


def _is_link_only(text: str) -> bool:
    value = _clean_text(text)
    if not value:
        return True
    if re.fullmatch(r"https?://\S+", value, flags=re.I):
        return True
    if re.fullmatch(r"(?:doi:\s*)?10\.\d{4,9}/\S+", value, flags=re.I):
        return True
    return False


class TelemarksforskingNoPublikasjonerCrawler(BaseCrawler):
    site_id = "telemarksforsking-no-publikasjoner"
    site_name = "Custom: telemarksforsking-no-publikasjoner"
    base_url = "https://telemarksforsking.no"

    def _curl_get(self, url: str, *, max_time: int = 60) -> str | None:
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--connect-timeout",
            "20",
            "--max-time",
            str(max_time),
            "-A",
            self.USER_AGENT,
            "-w",
            "\n__HTTP_STATUS__:%{http_code}",
            url,
        ]
        for attempt, wait in enumerate(_RETRY_WAITS, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=max_time + 10)
                text = result.stdout.decode("utf-8", errors="replace")
                status = None
                if "__HTTP_STATUS__:" in text:
                    text, _, status_part = text.rpartition("\n__HTTP_STATUS__:")
                    status = status_part.strip()
                if result.returncode == 0 and status and status.startswith(("2", "3")) and text.strip():
                    return text
                print(
                    f"[{self.site_id}] curl failed for {url} "
                    f"(attempt {attempt}/3, status={status}, rc={result.returncode})"
                )
            except Exception as exc:
                print(f"[{self.site_id}] curl error for {url} (attempt {attempt}/3): {exc}")
            if attempt < 3:
                time.sleep(wait)
        print(f"[{self.site_id}] curl failed after 3 attempts: {url}")
        return None

    def _curl_head(self, url: str, *, max_time: int = 30) -> str | None:
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skIL",
            "--connect-timeout",
            "15",
            "--max-time",
            str(max_time),
            "-A",
            self.USER_AGENT,
            url,
        ]
        for attempt, wait in enumerate(_RETRY_WAITS, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=max_time + 10)
                text = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and text.strip():
                    return text
                print(f"[{self.site_id}] HEAD failed for {url} (attempt {attempt}/3)")
            except Exception as exc:
                print(f"[{self.site_id}] HEAD error for {url} (attempt {attempt}/3): {exc}")
            if attempt < 3:
                time.sleep(wait)
        return None

    def _list_url(self, page: int) -> str:
        if page <= 1:
            return _START_URL
        return f"{self.base_url}/publikasjoner/tab/?e-page-{_LIST_WIDGET_ID}={page}&tab=vitenskapelig"

    def _parse_list_page(self, raw: str, page_url: str) -> tuple[list[dict], int | None, bool]:
        soup = _make_soup(raw)
        if soup is None:
            return [], None, False

        widget = soup.select_one(f'[data-id="{_LIST_WIDGET_ID}"][data-widget_type*="loop-grid"]')
        if widget is None:
            widget = soup.select_one(f".elementor-element-{_LIST_WIDGET_ID}")
        if widget is None:
            print(f"[{self.site_id}] list widget {_LIST_WIDGET_ID} not found: {page_url}")
            return [], None, False

        max_page = None
        anchor = widget.select_one(".e-load-more-anchor")
        if anchor and anchor.get("data-max-page"):
            try:
                max_page = int(anchor.get("data-max-page"))
            except (TypeError, ValueError):
                max_page = None

        has_next = bool(widget.select_one("a.page-numbers.next[href]"))
        if anchor and anchor.get("data-next-page"):
            has_next = True

        records: list[dict] = []
        for item in widget.select("div.e-loop-item.publication"):
            wp_post_id = _wp_post_id_from_classes(item)
            hrefs: list[str] = []
            for link in item.select("a[href]"):
                href = urljoin(page_url, link.get("href"))
                if re.search(
                    r"/(?:en/)?(?:publikasjoner/.+/\d+|publication/.+)/?$",
                    urlparse(href).path,
                ):
                    hrefs.append(href)
            hrefs = _dedupe(hrefs)
            detail_url = next((href for href in hrefs if "/en/" in urlparse(href).path), None)
            detail_url = detail_url or (hrefs[0] if hrefs else None)
            if not detail_url:
                continue

            title_node = item.select_one("h3.elementor-heading-title")
            title = _clean_text(title_node.get_text(" ", strip=True) if title_node else "")
            category_no = _clean_text(
                item.select_one(".hideen .elementor-heading-title").get_text(" ", strip=True)
                if item.select_one(".hideen .elementor-heading-title")
                else ""
            )
            category_en = _clean_text(
                item.select_one(".hideno .elementor-heading-title").get_text(" ", strip=True)
                if item.select_one(".hideno .elementor-heading-title")
                else ""
            )

            date_raw = None
            for node in item.select(".elementor-button-text"):
                text = _clean_text(node.get_text(" ", strip=True))
                if text:
                    date_raw = text
                    break

            post_number = _post_number_from_url(detail_url)
            records.append(
                {
                    "wp_post_id": wp_post_id,
                    "external_id": wp_post_id or post_number or detail_url,
                    "post_number": post_number,
                    "title": title,
                    "url": detail_url,
                    "listed_date_raw": date_raw,
                    "listed_date": _parse_date(date_raw),
                    "category": category_no or category_en or None,
                    "category_no": category_no or None,
                    "category_en": category_en or None,
                    "list_endpoint": page_url,
                }
            )
        return records, max_page, has_next

    def _ordered_heading_texts(self, root) -> list[str]:
        texts: list[str] = []
        for node in root.select(".elementor-heading-title, .elementor-button-text"):
            text = _clean_text(node.get_text(" ", strip=True))
            if text:
                texts.append(text)
        return texts

    def _value_after(self, texts: list[str], candidates: set[str]) -> str | None:
        for idx, text in enumerate(texts):
            if _label(text) not in candidates:
                continue
            if idx + 1 >= len(texts):
                return None
            value = texts[idx + 1]
            if _label(value) in _LABELS:
                return None
            return value
        return None

    def _extract_abstract(self, root) -> str:
        node = root.select_one('[data-id="1800ad7"][data-widget_type="text-editor.default"]')
        if node is None:
            node = root.select_one(".elementor-widget-text-editor")
        if node is None:
            return ""

        parts: list[str] = []
        for child in node.find_all(["p", "li"], recursive=True):
            text = _clean_text(child.get_text(" ", strip=True))
            if not text:
                continue
            if _label(text) in {"abstract", "sammendrag"}:
                continue
            if _is_link_only(text):
                continue
            parts.append(text)

        if not parts:
            raw_text = node.get_text("\n", strip=True)
            for line in raw_text.splitlines():
                text = _clean_text(line)
                if not text or _label(text) in {"abstract", "sammendrag"} or _is_link_only(text):
                    continue
                parts.append(text)

        return "\n\n".join(_dedupe(parts))

    def _extract_pdf_url(self, root, base_url: str) -> str | None:
        for link in root.select("a[href]"):
            href = urljoin(base_url, link.get("href"))
            text = _clean_text(link.get_text(" ", strip=True)).lower()
            if _looks_like_pdf_url(href):
                return href
            if text in {"download", "last ned"} and href and not href.startswith("#"):
                parsed = urlparse(href)
                if not parsed.netloc.endswith("doi.org") and "hdl.handle.net" not in parsed.netloc:
                    return href
        return None

    def _parse_detail(self, raw: str, list_item: dict) -> dict | None:
        soup = _make_soup(raw)
        if soup is None:
            return None

        detail_url = list_item["url"]
        canonical_url = _canonical_page_url(soup, detail_url)
        root = soup.select_one('[data-elementor-type="single-post"]') or soup

        # Skip WordPress 404 shells that still return HTML.
        page_title = _clean_text((soup.find("title").get_text(" ", strip=True) if soup.find("title") else ""))
        if "ikke funnet" in page_title.lower() or "not found" in page_title.lower():
            print(f"[{self.site_id}] detail returned not-found page: {detail_url}")
            return None

        title_node = root.select_one("h1.elementor-heading-title")
        title = _clean_text(title_node.get_text(" ", strip=True) if title_node else "") or list_item.get("title")
        texts = self._ordered_heading_texts(root)

        released_raw = self._value_after(texts, {"released", "utgitt"})
        published_date = _parse_date(released_raw) or list_item.get("listed_date")
        isbn = self._value_after(texts, {"isbn"})
        if isbn and _label(isbn) in {"publication", "type"}:
            isbn = None
        category = (
            self._value_after(texts, {"publication", "type"})
            or list_item.get("category")
        )
        department = self._value_after(texts, {"research group", "research groups", "forskergruppe", "forskergrupper"})
        authors_raw = self._value_after(texts, {"author", "authors", "forfatter", "forfattere"})
        authors = _split_authors(authors_raw)
        publisher = self._value_after(texts, {"publisher", "utgiver", "forlag"}) or "Telemarksforsking"
        journal_raw = self._value_after(texts, {"journal", "tidsskrift"})
        series = self._value_after(texts, {"series", "serie"})
        volume = self._value_after(texts, {"volume", "volum"})
        issue = self._value_after(texts, {"issue", "nummer"})

        abstract = self._extract_abstract(root)
        all_detail_text = root.get_text(" ", strip=True)
        doi = None
        for link in root.select("a[href]"):
            doi = _extract_doi(link.get("href")) or _extract_doi(link.get_text(" ", strip=True))
            if doi:
                break
        doi = doi or _extract_doi(all_detail_text)

        keyword_meta = soup.find("meta", attrs={"name": "keywords"})
        keywords = _clean_text(keyword_meta.get("content") if keyword_meta else "") or None

        pdf_url = self._extract_pdf_url(root, canonical_url)
        original_filename = None
        if pdf_url:
            original_filename = (
                _filename_from_headers(self._curl_head(pdf_url))
                or _filename_from_pdf_url(pdf_url)
            )

        wp_post_id = None
        single_id = _wp_post_id_from_classes(root)
        wp_post_id = single_id or list_item.get("wp_post_id")
        post_number = _post_number_from_url(canonical_url) or list_item.get("post_number")

        rest_link = None
        for link in soup.find_all("link", href=True):
            href = link.get("href")
            if "/wp-json/wp/v2/publication/" in href:
                rest_link = urljoin(canonical_url, href)
                break

        metadata = {
            "posted_date": list_item.get("listed_date_raw"),
            "listed_date": list_item.get("listed_date"),
            "originalFilename": original_filename,
            "journal_raw": journal_raw,
            "series": series,
            "volume": volume,
            "issue": issue,
            "wp_post_id": wp_post_id,
            "node_id": wp_post_id,
            "publication_number": post_number,
            "post_number": post_number,
            "wordpress_rest_url": rest_link or (f"{_REST_COLLECTION}/{wp_post_id}" if wp_post_id else None),
            "rest_collection": _REST_COLLECTION,
            "elementor_loop_id": _LIST_WIDGET_ID,
            "list_endpoint": list_item.get("list_endpoint"),
            "list_title": list_item.get("title"),
            "list_category": list_item.get("category"),
            "list_category_no": list_item.get("category_no"),
            "list_category_en": list_item.get("category_en"),
            "list_date_raw": list_item.get("listed_date_raw"),
            "detail_released_raw": released_raw,
            "isbn": isbn,
            "research_group": department,
            "detail_category": category,
            "canonical_url": canonical_url,
        }

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": str(wp_post_id or list_item.get("external_id") or post_number or canonical_url),
            "post_number": str(post_number) if post_number is not None else None,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": list_item.get("listed_date"),
            "posted_date": list_item.get("listed_date"),
            "authors": authors,
            "publisher": publisher,
            "department": department,
            "journal": journal_raw,
            "url": canonical_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def crawl(self, limit=None):
        started_at = time.monotonic()
        saved = 0
        page = 1
        seen_urls: set[str] = set()
        max_page_seen: int | None = None
        limit_label = str(limit) if limit is not None else "inf"

        while page <= _PAGE_CAP:
            if limit is not None and saved >= limit:
                break

            elapsed = time.monotonic() - started_at
            if elapsed >= _CRAWL_BUDGET_SECONDS - 30:
                print(f"[{self.site_id}] approaching 25 minute crawl budget; exiting cleanly")
                break

            if page == 1 or page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            list_url = self._list_url(page)
            raw = self._curl_get(list_url)
            if not raw:
                print(f"[{self.site_id}] list page fetch failed at page {page}; stopping")
                break

            items, page_max, has_next = self._parse_list_page(raw, list_url)
            if page_max:
                max_page_seen = page_max

            if not items:
                print(f"[{self.site_id}] page {page} returned 0 records; stopping")
                break

            new_records_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                item_label = item.get("post_number") or item.get("wp_post_id") or item.get("url") or "unknown"
                detail_url = item.get("url")
                if not detail_url:
                    continue
                seen_key = _canonical_seen_url(detail_url)
                if seen_key in seen_urls:
                    continue
                seen_urls.add(seen_key)
                new_records_on_page += 1

                try:
                    time.sleep(self._delay)
                    detail_raw = self._curl_get(detail_url)
                    if not detail_raw:
                        print(f"[{self.site_id}] item {item_label} detail fetch failed; skipping")
                        continue

                    paper = self._parse_detail(detail_raw, item)
                    if not paper:
                        continue

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_label}: {paper['title'][:80]}")
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if new_records_on_page == 0:
                print(f"[{self.site_id}] page {page} had 0 new URLs; stopping")
                break

            if limit is not None and saved >= limit:
                break

            if max_page_seen is not None and page >= max_page_seen:
                print(f"[{self.site_id}] reached last reported page {max_page_seen}")
                break

            if not has_next:
                print(f"[{self.site_id}] next page link absent at page {page}; stopping")
                break

            page += 1
        else:
            print(f"[{self.site_id}] reached safety page cap {_PAGE_CAP}")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
