# -*- coding: utf-8 -*-
"""Crawler for PTB-Berichte on ptb.de CMS.

Discovery notes:
- The requested PTB page is a TYPO3 news HTML list.
- The TYPO3 ``tx_news_pi1[news]`` detail URL is the CMS-native record URL, but
  it renders the same list body for this page.
- Each record links to a DOI, and the DOI resolves to the PTB Open Access
  Repository metadata page. The OAR page carries citation/DC meta tags,
  abstract text, PDF URL, dates, series data, and keywords.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

# Absolute import: spec_from_file_location has no package context.
_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from crawler.base_crawler import BaseCrawler  # noqa: E402


def _make_soup(raw: str | bytes):
    """Parse HTML with fallback chain html5lib -> lxml -> html.parser."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup

            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "get_text"):
        text = value.get_text(" ", strip=True)
    else:
        text = str(value)
    text = unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _meta_values(soup, name: str) -> list[str]:
    values = []
    for tag in soup.find_all("meta", {"name": name}):
        value = (tag.get("content") or "").strip()
        if value:
            values.append(unescape(value))
    return values


def _first_meta(soup, *names: str) -> str | None:
    for name in names:
        values = _meta_values(soup, name)
        for value in values:
            if value:
                return value
    return None


def _normalize_date(value: str | None) -> str | None:
    """Best-effort ISO date normalization for YYYY, YYYY-MM, DD.MM.YYYY."""
    if not value:
        return None
    value = _clean_text(value)
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"(\d{4})-(\d{2})\b", value)
    if m:
        return f"{m.group(1)}-{m.group(2)}-01"
    m = re.search(r"\b(\d{2})\.(\d{2})\.(\d{4})\b", value)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m = re.fullmatch(r"\d{4}", value)
    if m:
        return f"{value}-01-01"
    return value


class PtbDeCmsCrawler(BaseCrawler):
    site_id = "ptb-de-cms"
    site_name = "Custom: ptb-de-cms"
    base_url = "https://www.ptb.de"

    _LIST_URL = (
        "https://www.ptb.de/cms/presseaktuelles/"
        "wissenschaftlich-technische-publikationen/ptb-berichte.html"
    )
    _OAR_BASE_URL = "https://oar.ptb.de"
    _PAGE_CAP = 200
    _MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _MIN_ABSTRACT_CHARS = 50
    _CURL_USER_AGENTS = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120 Safari/537.36",
        "Mozilla/5.0",
    )

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, retries: int = 3) -> str | None:
        """Fetch a URL with curl, TLS cap, retries, and replacement decoding."""
        waits = [1, 3, 9]
        for attempt in range(retries):
            user_agent = self._CURL_USER_AGENTS[min(attempt, len(self._CURL_USER_AGENTS) - 1)]
            if attempt:
                wait = waits[min(attempt - 1, len(waits) - 1)]
                print(f"[ptb-de-cms] retry {attempt + 1}/{retries} in {wait}s: {url}")
                time.sleep(wait)
            time.sleep(self._delay)
            try:
                result = subprocess.run(
                    [
                        "curl",
                        "--tls-max",
                        "1.3",
                        "-sk",
                        "-L",
                        "--max-time",
                        "45",
                        "-A",
                        user_agent,
                        "-H",
                        "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
                        url,
                    ],
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
                if result.returncode == 0 and result.stdout:
                    body = result.stdout.decode("utf-8", errors="replace")
                    if "Web Page Blocked!" not in body and "Webseite blockiert!" not in body:
                        return body
                    print(f"[ptb-de-cms] blocked response ({attempt + 1}/{retries}) for {url}")
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                print(
                    f"[ptb-de-cms] curl failed ({attempt + 1}/{retries}) "
                    f"code={result.returncode}: {stderr[:200]}"
                )
            except Exception as exc:
                print(f"[ptb-de-cms] curl error ({attempt + 1}/{retries}) for {url}: {exc}")
        return None

    def _curl_head(self, url: str, retries: int = 3) -> str | None:
        """Fetch response headers, mainly for Content-Disposition filenames."""
        waits = [1, 3, 9]
        for attempt in range(retries):
            user_agent = self._CURL_USER_AGENTS[min(attempt, len(self._CURL_USER_AGENTS) - 1)]
            if attempt:
                wait = waits[min(attempt - 1, len(waits) - 1)]
                print(f"[ptb-de-cms] retry HEAD {attempt + 1}/{retries} in {wait}s: {url}")
                time.sleep(wait)
            time.sleep(self._delay)
            try:
                result = subprocess.run(
                    [
                        "curl",
                        "--tls-max",
                        "1.3",
                        "-sk",
                        "-I",
                        "-L",
                        "--max-time",
                        "30",
                        "-A",
                        user_agent,
                        url,
                    ],
                    capture_output=True,
                    timeout=45,
                    check=False,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                print(
                    f"[ptb-de-cms] curl HEAD failed ({attempt + 1}/{retries}) "
                    f"code={result.returncode}: {stderr[:200]}"
                )
            except Exception as exc:
                print(f"[ptb-de-cms] curl HEAD error ({attempt + 1}/{retries}) for {url}: {exc}")
        return None

    # ------------------------------------------------------------------
    # List parsing
    # ------------------------------------------------------------------

    def _list_url(self, page: int) -> str:
        if page <= 1:
            return self._LIST_URL
        # TYPO3 news commonly uses this widget parameter. The crawler prefers
        # actual next links when present and stops if page 1 has no next link.
        return f"{self._LIST_URL}?tx_news_pi1%5B%40widget_0%5D%5BcurrentPage%5D={page}"

    def _parse_list_page(self, html: str) -> list[dict[str, Any]]:
        soup = _make_soup(html)
        if not soup:
            return []
        main = soup.find("main", id="maincontent") or soup
        items = []
        for article in main.select("div.news-list-view article.article"):
            link = article.select_one("h2 a[href*='tx_news_pi1']")
            if not link:
                continue
            cms_url = urljoin(self._LIST_URL, link.get("href", "").strip())
            title = _clean_text(link)
            parsed_qs = parse_qs(urlparse(cms_url).query)
            news_id = (parsed_qs.get("tx_news_pi1[news]") or [""])[0].strip()

            time_tag = article.find("time")
            listed_raw = _clean_text(time_tag) if time_tag else ""
            listed_date = (
                time_tag.get("datetime").strip()
                if time_tag and time_tag.get("datetime")
                else _normalize_date(listed_raw)
            )

            teaser = article.find("div", class_="teaser-text")
            authors = ""
            series = ""
            isbn = ""
            doi = ""
            doi_url = ""
            if teaser:
                paragraphs = [_clean_text(p) for p in teaser.find_all("p")]
                paragraphs = [p for p in paragraphs if p and "[ mehr ]" not in p]
                if paragraphs:
                    authors = paragraphs[0]
                teaser_text = " ".join(paragraphs)
                series_match = re.search(r"\bPTB-Bericht\s+([A-Za-z]+-\d+)\b", teaser_text)
                if series_match:
                    series = f"PTB-Bericht {series_match.group(1)}"
                isbn_match = re.search(r"\bISBN\s+([0-9Xx][0-9Xx\-\s]+)", teaser_text)
                if isbn_match:
                    isbn = isbn_match.group(1).strip()
                doi_link = teaser.find("a", href=re.compile(r"doi\.org/10\.7795/"))
                if doi_link:
                    doi_url = doi_link.get("href", "").strip()
                    doi_match = re.search(r"doi\.org/(10\.7795/[^?#\s]+)", doi_url)
                    if doi_match:
                        doi = doi_match.group(1).rstrip(".,")

            items.append(
                {
                    "news_id": news_id or None,
                    "post_number": news_id or None,
                    "cms_detail_url": cms_url,
                    "url": doi_url or cms_url,
                    "title": title,
                    "authors_listed": authors,
                    "listed_date": listed_date,
                    "posted_date_raw": listed_raw,
                    "series": series,
                    "isbn": isbn,
                    "doi": doi or None,
                    "doi_url": doi_url or None,
                    "category": "PTB-Bericht",
                    "raw_query": parsed_qs,
                }
            )
        return items

    def _parse_next_url(self, html: str) -> str | None:
        soup = _make_soup(html)
        if not soup:
            return None
        main = soup.find("main", id="maincontent") or soup
        selectors = (
            ".f3-widget-paginator a.next",
            ".f3-widget-paginator li.next a",
            ".pagination a.next",
            ".pagination li.next a",
            "a[rel='next']",
        )
        for selector in selectors:
            link = main.select_one(selector)
            if link and link.get("href"):
                return urljoin(self._LIST_URL, link["href"])
        for link in main.find_all("a", href=True):
            text = _clean_text(link).lower()
            href = link["href"]
            if text in {"weiter", "next", ">", "›"} and "ptb-berichte" in href:
                return urljoin(self._LIST_URL, href)
        return None

    # ------------------------------------------------------------------
    # OAR detail parsing
    # ------------------------------------------------------------------

    def _table_fields(self, soup) -> dict[str, str]:
        fields: dict[str, str] = {}
        for tr in soup.select("#resource-details tr"):
            cells = tr.find_all("td")
            if len(cells) < 2:
                continue
            key = _clean_text(cells[0]).rstrip(":")
            value_cell = cells[1]
            for hidden in value_cell.select(".uk-hidden\\@s"):
                hidden.decompose()
            value = _clean_text(value_cell)
            if key:
                fields[key] = value
        return fields

    def _parse_oar_detail(self, detail_url: str, html: str) -> dict[str, Any]:
        soup = _make_soup(html)
        if not soup:
            return {}
        fields = self._table_fields(soup)
        canonical = None
        canonical_tag = soup.find("link", rel="canonical")
        if canonical_tag and canonical_tag.get("href"):
            canonical = urljoin(self._OAR_BASE_URL, canonical_tag["href"])

        title = _first_meta(soup, "citation_title", "DC.title") or fields.get("Titel")
        abstracts = _meta_values(soup, "citation_abstract")
        abstracts.extend(v for v in _meta_values(soup, "DC.description") if len(v) > 80)
        abstract = max(abstracts, key=len) if abstracts else fields.get("Zusammenfassung", "")

        authors = _meta_values(soup, "citation_author") or _meta_values(soup, "DC.creator")
        publisher = _first_meta(soup, "DC.publisher") or fields.get("Verlag")
        journal_raw = _first_meta(soup, "citation_journal_title")
        keywords = _meta_values(soup, "DC.subject")

        doi = None
        dc_identifier = _first_meta(soup, "DC.identifier")
        if dc_identifier:
            doi_match = re.search(r"(10\.7795/[^\s]+)", dc_identifier)
            if doi_match:
                doi = doi_match.group(1).rstrip(".,")
        if not doi and fields.get("DOI"):
            doi = fields["DOI"].strip()

        pdf_url = _first_meta(soup, "citation_pdf_url")
        if not pdf_url:
            link = soup.find("a", href=re.compile(r"/files/download/"))
            if link:
                pdf_url = urljoin(self._OAR_BASE_URL, link.get("href", ""))

        date_blob = fields.get("Datumsangaben", "")
        available_raw = None
        created_raw = None
        available_match = re.search(r"Verfügbar:\s*([0-9]{4}(?:-[0-9]{2})?(?:-[0-9]{2})?)", date_blob)
        if available_match:
            available_raw = available_match.group(1)
        created_match = re.search(r"Erstellt:\s*([0-9]{4}(?:-[0-9]{2})?(?:-[0-9]{2})?)", date_blob)
        if created_match:
            created_raw = created_match.group(1)

        publication_raw = (
            created_raw
            or _first_meta(soup, "citation_publication_date", "DC.date")
            or fields.get("Erscheinungsjahr")
        )
        published_date = _normalize_date(publication_raw)

        series = fields.get("Schriftenreihe") or fields.get("Information zur Reihe")
        if series:
            series = re.sub(r"\s*;\s*", " ", series).strip()
        resource_type = fields.get("Art der Ressource")
        relationships = fields.get("Beziehungen")
        rights = fields.get("Rechte")
        language = fields.get("Sprachen")
        pages = fields.get("Seiten")
        citation = fields.get("Zitat")

        oar_resource_id = None
        if canonical:
            oar_resource_id = canonical.rstrip("/").rsplit("/", 1)[-1]
        pdf_file_id = None
        if pdf_url:
            pdf_file_id = pdf_url.rstrip("/").rsplit("/", 1)[-1]

        citation_meta = {}
        dc_meta = {}
        for tag in soup.find_all("meta"):
            name = tag.get("name")
            content = tag.get("content")
            if not name or content is None:
                continue
            bucket = citation_meta if name.startswith("citation_") else dc_meta if name.startswith("DC.") else None
            if bucket is None:
                continue
            bucket.setdefault(name, []).append(unescape(content.strip()))

        return {
            "title": title,
            "abstract": _clean_text(abstract),
            "authors": "; ".join(a for a in authors if a),
            "publisher": publisher,
            "journal": journal_raw,
            "journal_raw": journal_raw,
            "keywords": ", ".join(k for k in keywords if k),
            "doi": doi,
            "pdf_url": pdf_url,
            "published_date": published_date,
            "publication_raw": publication_raw,
            "available_date": _normalize_date(available_raw),
            "available_raw": available_raw,
            "created_raw": created_raw,
            "series": series,
            "resource_type": resource_type,
            "relationships": relationships,
            "rights": rights,
            "language": language,
            "pages": pages,
            "citation": citation,
            "canonical_url": canonical or detail_url,
            "oar_resource_id": oar_resource_id,
            "pdf_file_id": pdf_file_id,
            "table_fields": fields,
            "citation_meta": citation_meta,
            "dc_meta": dc_meta,
        }

    def _filename_from_headers(self, pdf_url: str | None) -> str | None:
        if not pdf_url:
            return None
        headers = self._curl_head(pdf_url)
        if headers:
            match = re.search(r"filename\*=UTF-8''([^;\r\n]+)", headers, re.I)
            if match:
                return unquote(match.group(1).strip().strip('"'))
            match = re.search(r'filename="([^"\r\n]+)"', headers, re.I)
            if match:
                return unquote(match.group(1).strip())
            match = re.search(r"filename=([^;\r\n]+)", headers, re.I)
            if match:
                return unquote(match.group(1).strip().strip('"'))
        tail = unquote(pdf_url.rstrip("/").split("/")[-1].split("?")[0])
        return tail or None

    def _build_paper(
        self,
        list_item: dict[str, Any],
        detail: dict[str, Any],
        original_filename: str | None,
    ) -> dict[str, Any]:
        news_id = list_item.get("news_id")
        doi = detail.get("doi") or list_item.get("doi")
        external_id = news_id or doi or detail.get("oar_resource_id")
        post_number = news_id or detail.get("oar_resource_id") or doi
        listed_date = list_item.get("listed_date")
        url = detail.get("canonical_url") or list_item.get("url") or list_item.get("cms_detail_url")
        series = detail.get("series") or list_item.get("series")

        metadata = {
            "posted_date": list_item.get("posted_date_raw") or listed_date,
            "listed_date": listed_date,
            "originalFilename": original_filename,
            "journal_raw": detail.get("journal_raw"),
            "series": series,
            "volume": None,
            "issue": None,
            "news_id": news_id,
            "tx_news_pi1_news": news_id,
            "post_number": post_number,
            "cms_detail_url": list_item.get("cms_detail_url"),
            "doi_url": list_item.get("doi_url"),
            "oar_url": detail.get("canonical_url"),
            "oar_resource_id": detail.get("oar_resource_id"),
            "pdf_file_id": detail.get("pdf_file_id"),
            "isbn": list_item.get("isbn"),
            "category": list_item.get("category"),
            "publication_raw": detail.get("publication_raw"),
            "available_date": detail.get("available_date"),
            "available_raw": detail.get("available_raw"),
            "created_raw": detail.get("created_raw"),
            "resource_type": detail.get("resource_type"),
            "relationships": detail.get("relationships"),
            "rights": detail.get("rights"),
            "language": detail.get("language"),
            "pages": detail.get("pages"),
            "citation": detail.get("citation"),
            "raw_query": list_item.get("raw_query"),
            "table_fields": detail.get("table_fields"),
            "citation_meta": detail.get("citation_meta"),
            "dc_meta": detail.get("dc_meta"),
            "list_raw": list_item,
        }

        return {
            "id": str(uuid.uuid4()),
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": detail.get("title") or list_item.get("title"),
            "abstract": detail.get("abstract"),
            "published_date": detail.get("published_date") or listed_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": detail.get("authors") or list_item.get("authors_listed"),
            "publisher": detail.get("publisher") or "Physikalisch-Technische Bundesanstalt (PTB)",
            "department": None,
            "journal": detail.get("journal"),
            "url": url,
            "pdf_url": detail.get("pdf_url"),
            "keywords": detail.get("keywords"),
            "category": list_item.get("category"),
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        if limit is not None and limit <= 0:
            return 0

        saved = 0
        limit_label = str(limit) if limit is not None else "inf"
        start = time.monotonic()
        seen_urls: set[str] = set()
        page = 1
        next_url: str | None = self._list_url(page)

        while next_url and page <= self._PAGE_CAP:
            if time.monotonic() - start >= self._MAX_SECONDS - 60:
                print("[ptb-de-cms] approaching 25 minute wall-clock budget; stopping cleanly")
                break
            if page % 10 == 0:
                print(f"[ptb-de-cms] page {page}: saved {saved}/{limit_label}")

            list_html = self._curl_get(next_url)
            if not list_html:
                print(f"[ptb-de-cms] page {page}: fetch failed")
                break

            items = self._parse_list_page(list_html)
            if not items:
                print(f"[ptb-de-cms] page {page}: no records")
                break

            new_items = []
            for item in items:
                item_url = item.get("url") or item.get("cms_detail_url")
                if not item_url:
                    continue
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                new_items.append(item)

            if not new_items:
                print(f"[ptb-de-cms] page {page}: 0 new records; stopping")
                break

            for index, item in enumerate(new_items, start=1):
                if limit is not None and saved >= limit:
                    break
                if time.monotonic() - start >= self._MAX_SECONDS - 60:
                    print("[ptb-de-cms] approaching 25 minute wall-clock budget; stopping cleanly")
                    return saved

                item_label = item.get("news_id") or item.get("doi") or item.get("url") or f"page{page}-{index}"
                try:
                    detail_url = item.get("doi_url") or item.get("url") or item.get("cms_detail_url")
                    if not detail_url:
                        print(f"[ptb-de-cms] item {item_label} missing detail URL; skipped")
                        continue
                    detail_html = self._curl_get(detail_url)
                    if not detail_html:
                        print(f"[ptb-de-cms] item {item_label} detail fetch failed; skipped")
                        continue
                    detail = self._parse_oar_detail(detail_url, detail_html)
                    original_filename = self._filename_from_headers(detail.get("pdf_url"))
                    paper = self._build_paper(item, detail, original_filename)
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self._MIN_ABSTRACT_CHARS:
                        print(
                            f"[ptb-de-cms] item {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue
                    self._save_paper(paper)
                    saved += 1
                except Exception as exc:
                    print(f"[ptb-de-cms] item {item_label} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break

            discovered_next = self._parse_next_url(list_html)
            if not discovered_next:
                break
            if discovered_next == next_url:
                print(f"[ptb-de-cms] page {page}: next link repeats current page; stopping")
                break
            next_url = discovered_next
            page += 1

        if page > self._PAGE_CAP:
            print(f"[ptb-de-cms] reached safety page cap {self._PAGE_CAP}; stopping")
        return saved
