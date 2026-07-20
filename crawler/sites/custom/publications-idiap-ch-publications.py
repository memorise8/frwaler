# -*- coding: utf-8 -*-
"""Crawler for publications.idiap.ch journal papers.

Discovery notes:
  - Start/list endpoint: https://publications.idiap.ch/publications/articles
  - Detail endpoint:     https://publications.idiap.ch/publications/show/{id}
  - Export endpoints exist at /export/publication/{id}/bibtex and /marc21,
    but the detail HTML already exposes the fields needed here.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.parse
from typing import Any

from crawler.base_crawler import BaseCrawler


_SITE_ID = "publications-idiap-ch-publications"
_BASE_URL = "https://publications.idiap.ch"
_START_URL = f"{_BASE_URL}/publications/articles"
_MAX_PAGES = 200
_CRAWL_BUDGET_SECS = 25 * 60
_MIN_ABSTRACT_CHARS = 100

_MONTHS = {
    "jan": "01",
    "january": "01",
    "feb": "02",
    "february": "02",
    "mar": "03",
    "march": "03",
    "apr": "04",
    "april": "04",
    "may": "05",
    "jun": "06",
    "june": "06",
    "jul": "07",
    "july": "07",
    "aug": "08",
    "august": "08",
    "sep": "09",
    "sept": "09",
    "september": "09",
    "oct": "10",
    "october": "10",
    "nov": "11",
    "november": "11",
    "dec": "12",
    "december": "12",
}


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _make_soup(raw: str):
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:
        print(f"[{_SITE_ID}] BeautifulSoup unavailable: {exc}")
        return None

    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw or "", parser)
        except Exception as exc:
            print(f"[{_SITE_ID}] BeautifulSoup parser {parser} failed: {exc}")
            continue
    return None


def _absolute_url(url: str | None) -> str | None:
    if not url:
        return None
    return urllib.parse.urljoin(_BASE_URL, url)


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urllib.parse.urlparse(url).path
    name = urllib.parse.unquote(os.path.basename(path))
    return name if name else None


def _normalise_doi(raw: str | None) -> str | None:
    if not raw:
        return None
    doi = urllib.parse.unquote(_clean_text(raw))
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.I)
    for _ in range(4):
        new = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.I)
        if new == doi:
            break
        doi = new
    doi = doi.strip().strip(".")
    return doi or None


def _iso_date_from_parts(year_raw: str | None, month_raw: str | None = None) -> str | None:
    year_match = re.search(r"(?:19|20)\d{2}", year_raw or "")
    if not year_match:
        return None
    year = year_match.group(0)
    month = "01"
    if month_raw:
        month_text = _clean_text(month_raw).lower()
        numeric = re.search(r"\b(1[0-2]|0?[1-9])\b", month_text)
        if numeric:
            month = f"{int(numeric.group(1)):02d}"
        else:
            month = _MONTHS.get(month_text[:3], _MONTHS.get(month_text, "01"))
    return f"{year}-{month}-01"


class PublicationsIdiapChPublicationsCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: publications-idiap-ch-publications"
    base_url = _BASE_URL

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, *, timeout: int = 45, context: str = "request") -> str | None:
        """Fetch URL via curl with TLS 1.3 cap and 1/3/9 second backoff."""
        waits = [1, 3, 9]
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--max-time",
            str(timeout),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            url,
        ]

        last_error = ""
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 10,
                )
                body = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and body.strip():
                    return body
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = stderr or f"curl exit {result.returncode}"
            except Exception as exc:
                last_error = str(exc)

            if attempt < 2:
                wait = waits[attempt]
                print(
                    f"[{self.site_id}] {context} failed attempt {attempt + 1}/3 "
                    f"for {url}: {last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] {context} failed after 3 attempts for {url}: {last_error}")
        return None

    # ------------------------------------------------------------------
    # List parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, raw: str) -> tuple[list[dict[str, Any]], str | None]:
        soup = _make_soup(raw)
        if soup is None:
            return self._parse_list_page_regex(raw), None

        items: list[dict[str, Any]] = []
        for block in soup.select("div.publication_summary"):
            title_link = block.select_one("span.title a[href*='/publications/show/']")
            if title_link is None:
                title_link = block.select_one("a[href*='/publications/show/']")
            if title_link is None:
                continue

            detail_url = _absolute_url(title_link.get("href"))
            external_id = self._id_from_url(detail_url)
            if not detail_url or not external_id:
                continue

            authors = [
                _clean_text(a.get_text(" ", strip=True))
                for a in block.select("span.author a")
                if _clean_text(a.get_text(" ", strip=True))
            ]
            pdf_url = None
            doi = None
            external_url = None
            for a in block.find_all("a", href=True):
                href = _absolute_url(a.get("href"))
                text = _clean_text(a.get_text(" ", strip=True)).lower()
                title = _clean_text(a.get("title")).lower()
                if href and ".pdf" in urllib.parse.urlparse(href).path.lower():
                    pdf_url = href
                elif href and ("doi" in text or "doi" in title or "dx.doi.org" in href or "doi.org" in href):
                    doi = _normalise_doi(a.get_text(" ", strip=True) or href)
                elif href and text == "url":
                    external_url = href

            text = _clean_text(block.get_text(" ", strip=True))
            year_match = re.findall(r"(?:19|20)\d{2}", text)
            list_year = year_match[-1] if year_match else None

            items.append(
                {
                    "external_id": external_id,
                    "post_number": external_id,
                    "title": _clean_text(title_link.get_text(" ", strip=True)),
                    "url": detail_url,
                    "authors": authors,
                    "pdf_url": pdf_url,
                    "doi": doi,
                    "external_url": external_url,
                    "listed_date_raw": list_year,
                    "listed_date": _iso_date_from_parts(list_year),
                    "list_text": text,
                }
            )

        return items, self._next_page_url(soup)

    def _parse_list_page_regex(self, raw: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        pattern = re.compile(
            r"<a\s+[^>]*href=[\"'](?P<href>https://publications\.idiap\.ch/publications/show/(?P<id>\d+))[\"'][^>]*>"
            r"(?P<title>.*?)</a>",
            re.I | re.S,
        )
        seen: set[str] = set()
        for match in pattern.finditer(raw or ""):
            detail_url = match.group("href")
            if detail_url in seen:
                continue
            seen.add(detail_url)
            title = re.sub(r"<[^>]+>", " ", match.group("title"))
            external_id = match.group("id")
            items.append(
                {
                    "external_id": external_id,
                    "post_number": external_id,
                    "title": _clean_text(title),
                    "url": detail_url,
                    "authors": [],
                    "pdf_url": None,
                    "doi": None,
                    "external_url": None,
                    "listed_date_raw": None,
                    "listed_date": None,
                    "list_text": "",
                }
            )
        return items

    def _next_page_url(self, soup) -> str | None:
        nav = soup.select_one("div.aligncenter")
        if nav is None:
            return None

        active = nav.find("b")
        active_text = _clean_text(active.get_text(" ", strip=True)) if active else ""
        if not active_text.isdigit():
            return None
        target = str(int(active_text) + 1)
        for link in nav.find_all("a", href=True):
            if _clean_text(link.get_text(" ", strip=True)) == target:
                return _absolute_url(link.get("href"))
        return None

    @staticmethod
    def _id_from_url(url: str | None) -> str | None:
        if not url:
            return None
        match = re.search(r"/publications/show/(\d+)", url)
        return match.group(1) if match else None

    # ------------------------------------------------------------------
    # Detail parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, raw: str, list_item: dict[str, Any]) -> dict[str, Any]:
        soup = _make_soup(raw)
        if soup is None:
            return self._parse_detail_regex(raw, list_item)

        field_text: dict[str, str] = {}
        field_links: dict[str, list[dict[str, str | None]]] = {}
        for tr in soup.select("table.publication_details tr"):
            tds = tr.find_all("td", recursive=False)
            if len(tds) < 2:
                continue
            label = _clean_text(tds[0].get_text(" ", strip=True)).rstrip(":")
            if not label or len(label) > 100:
                continue
            key = label.lower()
            value_cell = tds[1]
            field_text[key] = _clean_text(value_cell.get_text(" ", strip=True))

            links = []
            for a in value_cell.find_all("a", href=True):
                links.append(
                    {
                        "text": _clean_text(a.get_text(" ", strip=True)),
                        "href": _absolute_url(a.get("href")),
                        "title": _clean_text(a.get("title")) or None,
                    }
                )
            if links:
                field_links[key] = links

        title = self._detail_title(soup) or list_item.get("title") or ""
        external_id = list_item.get("external_id") or self._id_from_url(list_item.get("url"))
        post_number = list_item.get("post_number") or external_id

        authors = [
            _clean_text(a.get_text(" ", strip=True))
            for a in soup.select("span.authorlist a")
            if _clean_text(a.get_text(" ", strip=True))
        ]
        if not authors:
            authors = list_item.get("authors") or []

        keywords = [
            _clean_text(link["text"])
            for link in field_links.get("keywords", [])
            if _clean_text(link.get("text"))
        ]
        if not keywords and field_text.get("keywords"):
            keywords = [
                _clean_text(part)
                for part in field_text["keywords"].split(",")
                if _clean_text(part)
            ]

        projects = [
            _clean_text(link["text"])
            for link in field_links.get("projects", [])
            if _clean_text(link.get("text"))
        ]

        main_program = field_text.get("main research program")
        additional_programs = field_text.get("additional research programs")
        department = "; ".join(
            p for p in (main_program, additional_programs) if p
        ) or None

        pdf_url = self._detail_pdf_url(soup) or list_item.get("pdf_url")
        original_filename = _filename_from_url(pdf_url)

        doi = self._detail_doi(field_text, field_links) or list_item.get("doi")
        external_url = self._detail_external_url(field_links) or list_item.get("external_url")
        year_raw = field_text.get("year") or list_item.get("listed_date_raw")
        month_raw = field_text.get("month")
        published_date = _iso_date_from_parts(year_raw, month_raw)
        listed_date = list_item.get("listed_date") or published_date
        listed_raw = list_item.get("listed_date_raw") or year_raw
        journal = field_text.get("journal") or None

        metadata = {
            "publication_id": external_id,
            "node_id": external_id,
            "post_number": post_number,
            "citation": field_text.get("citation"),
            "publication_status": field_text.get("publication status"),
            "posted_date": listed_raw,
            "listed_date": listed_date,
            "originalFilename": original_filename,
            "journal_raw": journal,
            "series": field_text.get("series"),
            "volume": field_text.get("volume"),
            "issue": field_text.get("issue"),
            "pages": field_text.get("pages"),
            "month": month_raw,
            "year": year_raw,
            "projects": projects,
            "main_research_program": main_program,
            "additional_research_programs": additional_programs,
            "external_url": external_url,
            "detail_fields": field_text,
            "detail_links": field_links,
            "list_record": list_item,
        }

        abstract = field_text.get("abstract") or ""

        return {
            "id": f"{self.site_id}:{external_id}" if external_id else None,
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": "; ".join(authors) if authors else None,
            "publisher": "Idiap Research Institute",
            "department": department,
            "journal": journal,
            "url": list_item.get("url"),
            "pdf_url": pdf_url,
            "keywords": ", ".join(keywords) if keywords else None,
            "category": field_text.get("type of publication") or "Journal paper",
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _parse_detail_regex(self, raw: str, list_item: dict[str, Any]) -> dict[str, Any]:
        title = list_item.get("title") or ""
        title_match = re.search(r"<div class=['\"]header['\"]>(.*?)</div>", raw or "", re.I | re.S)
        if title_match:
            title = _clean_text(re.sub(r"<[^>]+>", " ", title_match.group(1)))

        abstract = ""
        abs_match = re.search(
            r"<td[^>]*>\s*Abstract:\s*</td>\s*<td[^>]*>(.*?)</td>",
            raw or "",
            re.I | re.S,
        )
        if abs_match:
            abstract = _clean_text(re.sub(r"<[^>]+>", " ", abs_match.group(1)))

        external_id = list_item.get("external_id") or self._id_from_url(list_item.get("url"))
        metadata = {
            "publication_id": external_id,
            "node_id": external_id,
            "post_number": external_id,
            "posted_date": list_item.get("listed_date_raw"),
            "listed_date": list_item.get("listed_date"),
            "originalFilename": _filename_from_url(list_item.get("pdf_url")),
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "list_record": list_item,
        }
        return {
            "id": f"{self.site_id}:{external_id}" if external_id else None,
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": external_id,
            "title": title,
            "abstract": abstract,
            "published_date": list_item.get("listed_date"),
            "listed_date": list_item.get("listed_date"),
            "posted_date": list_item.get("listed_date"),
            "authors": "; ".join(list_item.get("authors") or []) or None,
            "publisher": "Idiap Research Institute",
            "department": None,
            "journal": None,
            "url": list_item.get("url"),
            "pdf_url": list_item.get("pdf_url"),
            "keywords": None,
            "category": "Journal paper",
            "doi": list_item.get("doi"),
            "original_filename": _filename_from_url(list_item.get("pdf_url")),
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    @staticmethod
    def _detail_title(soup) -> str | None:
        publication = soup.select_one("div.publication")
        if publication:
            header = publication.select_one("div.header")
            if header:
                title = _clean_text(header.get_text(" ", strip=True))
                if title:
                    return title
        return None

    @staticmethod
    def _detail_pdf_url(soup) -> str | None:
        for a in soup.select("ul.attachmentlist a[href]"):
            href = _absolute_url(a.get("href"))
            if href and ".pdf" in urllib.parse.urlparse(href).path.lower():
                return href
        for a in soup.find_all("a", href=True):
            href = _absolute_url(a.get("href"))
            if href and ".pdf" in urllib.parse.urlparse(href).path.lower():
                return href
        return None

    @staticmethod
    def _detail_doi(
        field_text: dict[str, str], field_links: dict[str, list[dict[str, str | None]]]
    ) -> str | None:
        for link in field_links.get("doi", []):
            doi = _normalise_doi(link.get("text") or link.get("href"))
            if doi:
                return doi
        return _normalise_doi(field_text.get("doi"))

    @staticmethod
    def _detail_external_url(field_links: dict[str, list[dict[str, str | None]]]) -> str | None:
        links = field_links.get("url") or []
        if links:
            return links[0].get("href")
        return None

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        if limit is not None and limit <= 0:
            return 0

        saved = 0
        seen_urls: set[str] = set()
        page_url = _START_URL
        start_time = time.monotonic()
        limit_or_inf = str(limit) if limit is not None else "inf"

        for page in range(1, _MAX_PAGES + 1):
            if limit is not None and saved >= limit:
                break

            elapsed = time.monotonic() - start_time
            if elapsed >= _CRAWL_BUDGET_SECS - 30:
                print(
                    f"[{self.site_id}] approaching 25-minute crawl budget "
                    f"at page {page}; saved {saved}/{limit_or_inf}"
                )
                break

            if page == 1 or page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")
            if page == _MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached")

            raw = self._curl_get(page_url, timeout=90, context=f"list page {page}")
            if not raw:
                print(f"[{self.site_id}] page {page}: empty list response; stopping")
                break

            list_items, next_url = self._parse_list_page(raw)
            if not list_items:
                print(f"[{self.site_id}] page {page}: 0 records; stopping")
                break

            new_items = [
                item for item in list_items
                if item.get("url") and item["url"] not in seen_urls
            ]
            if not new_items:
                print(f"[{self.site_id}] page {page}: 0 new records; stopping")
                break

            for item_index, item in enumerate(new_items, start=1):
                if limit is not None and saved >= limit:
                    break
                if time.monotonic() - start_time >= _CRAWL_BUDGET_SECS - 30:
                    print(
                        f"[{self.site_id}] approaching 25-minute crawl budget "
                        f"during detail fetch; saved {saved}/{limit_or_inf}"
                    )
                    return saved

                detail_url = item.get("url")
                seen_urls.add(detail_url)
                item_label = item.get("external_id") or detail_url or str(item_index)

                try:
                    detail_raw = self._curl_get(
                        detail_url,
                        timeout=45,
                        context=f"item {item_label}",
                    )
                    if not detail_raw:
                        raise RuntimeError("empty detail response")

                    paper = self._parse_detail(detail_raw, item)
                    abstract = _clean_text(paper.get("abstract"))
                    if len(abstract) < _MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue
                    paper["abstract"] = abstract

                    self._save_paper(paper)
                    saved += 1
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue
                finally:
                    if self._delay:
                        time.sleep(self._delay)

            if limit is not None and saved >= limit:
                break
            if not next_url:
                print(f"[{self.site_id}] page {page}: next page link absent; stopping")
                break
            page_url = next_url

        return saved
