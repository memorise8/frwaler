# -*- coding: utf-8 -*-
"""Crawler for Geneva Graduate Institute library publications.

Discovery notes:
  - Start page:
    https://www.graduateinstitute.ch/library/publications-institute
  - Drupal Views list route:
    https://www.graduateinstitute.ch/catalog-publication?page=N
    where page=0 is the first page and each page currently carries 12 cards.
  - Repository detail/export endpoint:
    https://repository.graduateinstitute.ch/record/{record_id}?of=xm
    returns MARCXML with title, abstract, DOI, authors, journal/source,
    collection terms, departments/centres, and PDF links.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any

from crawler.base_crawler import BaseCrawler


_SITE_ID = "graduateinstitute-ch-library"
_BASE_URL = "https://www.graduateinstitute.ch"
_START_URL = f"{_BASE_URL}/library/publications-institute"
_LIST_URL = f"{_BASE_URL}/catalog-publication"
_REPOSITORY_BASE = "https://repository.graduateinstitute.ch"
_MAX_PAGES = 200
_CRAWL_BUDGET_SECS = 25 * 60
_MIN_ABSTRACT_CHARS = 100


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


def _absolute_url(url: str | None, base: str = _BASE_URL) -> str | None:
    if not url:
        return None
    return urllib.parse.urljoin(base, url)


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urllib.parse.urlparse(url).path
    name = urllib.parse.unquote(os.path.basename(path.rstrip("/")))
    return name or None


def _normalise_doi(raw: str | None) -> str | None:
    if not raw:
        return None
    doi = urllib.parse.unquote(_clean_text(raw))
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.I)
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.I)
    doi = doi.strip().strip(".")
    return doi or None


def _normalise_date(raw: str | None) -> str | None:
    """Return YYYY-MM-DD when possible.

    The site often exposes publication dates as a bare year. Those are kept
    sortable by normalising to the first day of the year while preserving the
    raw value in metadata.
    """
    text = _clean_text(raw)
    if not text:
        return None

    iso_match = re.search(r"((?:19|20)\d{2})[-/](0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])", text)
    if iso_match:
        year, month, day = iso_match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"

    year_match = re.search(r"(?:19|20)\d{2}", text)
    if year_match:
        return f"{year_match.group(0)}-01-01"

    return None


def _date_from_marc_005(raw: str | None) -> str | None:
    text = _clean_text(raw)
    if len(text) >= 8 and text[:8].isdigit():
        return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"
    return None


def _join_title_parts(main: str, subtitle: str) -> str:
    main = _clean_text(main).rstrip()
    subtitle = _clean_text(subtitle).strip()
    if not subtitle:
        return main
    if not main:
        return subtitle
    if main.endswith(":"):
        return f"{main} {subtitle}"
    if main.endswith(("?", "!", ";")):
        return f"{main} {subtitle}"
    return f"{main}: {subtitle}"


class GraduateInstituteChLibraryCrawler(BaseCrawler):
    site_id = "graduateinstitute-ch-library"
    site_name = "Custom: graduateinstitute-ch-library"
    base_url = "https://www.graduateinstitute.ch"

    def __init__(self, db_conn, delay: float = 1.0, detail_delay: float | None = None):
        super().__init__(db_conn=db_conn, delay=delay)
        self._detail_delay = 1.0 if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(
        self,
        url: str,
        *,
        accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        timeout: int = 45,
        context: str = "request",
    ) -> str | None:
        """Fetch a URL with curl and 1s/3s/9s retry backoff."""
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
            f"Accept: {accept}",
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

    def _list_url(self, page_index: int) -> str:
        if page_index <= 0:
            return _START_URL
        return f"{_LIST_URL}?page={page_index}"

    def _parse_list_page(self, raw: str) -> tuple[list[dict[str, Any]], bool]:
        soup = _make_soup(raw)
        if soup is None:
            return self._parse_list_page_regex(raw), bool(re.search(r'href=["\']\?page=\d+', raw or ""))

        items: list[dict[str, Any]] = []
        for row in soup.select(".views-row"):
            link = row.select_one('a[href*="/library/publications-institute/"]')
            if link is None:
                continue

            url = _absolute_url(link.get("href"))
            if not url:
                continue

            category_el = row.select_one('[class*="field--name-field-publication-type"] .field__item')
            date_el = row.select_one('[class*="field--name-field-publication-date"] .field__item')
            title_el = row.select_one('[class*="field--name-iheid-field-title"] h1, [class*="field--name-iheid-field-title"] h2, [class*="field--name-iheid-field-title"] h3, [class*="field--name-iheid-field-title"] h4, [class*="field--name-iheid-field-title"] .field__item')
            image = row.select_one('img[src*="publication_"], img[src*="thumb-publication_"]')

            record_id = None
            if image and image.get("src"):
                record_match = re.search(r"(?:thumb-publication_|publication_)(\d+)", image.get("src") or "")
                if record_match:
                    record_id = record_match.group(1)

            authors = self._parse_card_authors(row)
            edition = self._parse_card_label_value(row, "Edition")
            list_date_raw = _clean_text(date_el.get_text(" ", strip=True)) if date_el else None

            items.append(
                {
                    "url": url,
                    "slug": urllib.parse.urlparse(url).path.rstrip("/").rsplit("/", 1)[-1],
                    "record_id": record_id,
                    "post_number": record_id,
                    "title": _clean_text(title_el.get_text(" ", strip=True)) if title_el else None,
                    "authors": authors,
                    "category": _clean_text(category_el.get_text(" ", strip=True)) if category_el else None,
                    "listed_date_raw": list_date_raw,
                    "listed_date": _normalise_date(list_date_raw),
                    "edition": edition,
                }
            )

        has_next = soup.select_one('[data-drupal-views-infinite-scroll-pager] a[rel="next"], .pager a[rel="next"]') is not None
        return items, has_next

    def _parse_list_page_regex(self, raw: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for match in re.finditer(r'href=["\'](?P<href>/library/publications-institute/[^"\']+)["\']', raw or "", re.I):
            url = _absolute_url(match.group("href"))
            if not url or url in seen:
                continue
            seen.add(url)
            record_match = re.search(r"(?:thumb-publication_|publication_)(\d+)", raw[max(0, match.start() - 3000): match.end() + 3000])
            record_id = record_match.group(1) if record_match else None
            items.append(
                {
                    "url": url,
                    "slug": urllib.parse.urlparse(url).path.rstrip("/").rsplit("/", 1)[-1],
                    "record_id": record_id,
                    "post_number": record_id,
                    "title": None,
                    "authors": [],
                    "category": None,
                    "listed_date_raw": None,
                    "listed_date": None,
                    "edition": None,
                }
            )
        return items

    def _parse_card_authors(self, row) -> list[str]:
        label = None
        for cite in row.select("cite"):
            if _clean_text(cite.get_text(" ", strip=True)).lower().rstrip(":") == "authors":
                label = cite
                break
        if label is None:
            return []

        value = label.find_next("div", class_=lambda cls: cls and "font-size--small" in cls)
        if value is None:
            return []

        text = _clean_text(value.get_text(" ", strip=True))
        text = re.sub(r"\s*\[\.\.\.\]\s*$", "", text).strip()
        if not text:
            return []
        return [_clean_text(part) for part in text.split(",") if _clean_text(part)]

    def _parse_card_label_value(self, row, label_text: str) -> str | None:
        expected = label_text.lower().rstrip(":")
        for cite in row.select("cite"):
            if _clean_text(cite.get_text(" ", strip=True)).lower().rstrip(":") != expected:
                continue
            value = cite.find_next("div", class_=lambda cls: cls and "font-size--small" in cls)
            text = _clean_text(value.get_text(" ", strip=True)) if value else ""
            return text or None
        return None

    # ------------------------------------------------------------------
    # Detail parsing
    # ------------------------------------------------------------------

    def _parse_institute_detail(self, raw: str, url: str) -> dict[str, Any]:
        soup = _make_soup(raw)
        if soup is None:
            record_match = re.search(r"repository\.graduateinstitute\.ch/record/(\d+)", raw or "")
            node_match = re.search(r'"currentPath"\s*:\s*"node/(\d+)"', raw or "")
            return {
                "record_id": record_match.group(1) if record_match else None,
                "node_id": node_match.group(1) if node_match else None,
                "title": None,
                "abstract": None,
                "authors": [],
                "publication_date_raw": None,
                "repository_url": None,
            }

        root = soup.select_one("main .iheid--entity--node--type-publication.iheid--entity--node--view-mode-full")
        if root is None:
            root = soup.select_one("main") or soup

        title_el = root.select_one('[class*="field--name-title"] h1, [class*="field--name-title"] h2, [class*="field--name-title"] h3, [class*="field--name-title"] .field__item')
        body_el = root.select_one('[class*="field--name-body"] [class*="text-default-formatter"], [class*="field--name-body"] .field__item')
        date_el = root.select_one('[class*="field--name-field-publication-date"] .field__item')
        cta = root.select_one('[class*="field--name-field-cta"] a[href*="repository.graduateinstitute.ch/record/"]')

        repository_url = cta.get("href") if cta else None
        record_match = re.search(r"/record/(\d+)", repository_url or "")
        if record_match is None:
            record_match = re.search(r"(?:thumb-publication_|publication_)(\d+)", raw or "")

        node_id = None
        settings = soup.select_one('script[type="application/json"][data-drupal-selector="drupal-settings-json"]')
        if settings:
            node_match = re.search(r'"currentPath"\s*:\s*"node/(\d+)"', settings.string or settings.get_text("", strip=False))
            if node_match:
                node_id = node_match.group(1)

        authors = [
            _clean_text(cite.get_text(" ", strip=True)).rstrip(":")
            for cite in root.select('[class*="field--name-authors-and-editors"] dd cite')
            if _clean_text(cite.get_text(" ", strip=True)).rstrip(":")
        ]

        return {
            "record_id": record_match.group(1) if record_match else None,
            "node_id": node_id,
            "title": _clean_text(title_el.get_text(" ", strip=True)) if title_el else None,
            "abstract": _clean_text(body_el.get_text(" ", strip=True)) if body_el else None,
            "authors": authors,
            "publication_date_raw": _clean_text(date_el.get_text(" ", strip=True)) if date_el else None,
            "repository_url": _absolute_url(repository_url, _REPOSITORY_BASE) if repository_url else None,
            "url": url,
        }

    def _fetch_repository_marc(self, record_id: str) -> dict[str, Any]:
        marc_url = f"{_REPOSITORY_BASE}/record/{record_id}?of=xm"
        raw = self._curl_get(
            marc_url,
            accept="application/xml,text/xml,*/*;q=0.8",
            timeout=45,
            context=f"repository MARC {record_id}",
        )
        if not raw:
            return {"repository_marc_url": marc_url}
        parsed = self._parse_marcxml(raw)
        parsed["repository_marc_url"] = marc_url
        parsed["repository_marc_raw"] = raw[:20000]
        return parsed

    def _parse_marcxml(self, raw: str) -> dict[str, Any]:
        try:
            root = ET.fromstring(raw.encode("utf-8", errors="replace"))
        except Exception as exc:
            print(f"[{self.site_id}] MARCXML parse failed: {exc}")
            return {}

        record = next((elem for elem in root.iter() if self._xml_local(elem.tag) == "record"), None)
        if record is None:
            return {}

        fields = [elem for elem in record if self._xml_local(elem.tag) == "datafield"]
        controlfields = [elem for elem in record if self._xml_local(elem.tag) == "controlfield"]

        def controls(tag: str) -> list[str]:
            return [_clean_text(elem.text) for elem in controlfields if elem.attrib.get("tag") == tag and _clean_text(elem.text)]

        def datafields(tag: str) -> list[Any]:
            return [elem for elem in fields if elem.attrib.get("tag") == tag]

        def subs(field, code: str | None = None) -> list[str]:
            values: list[str] = []
            for child in field:
                if self._xml_local(child.tag) != "subfield":
                    continue
                if code is not None and child.attrib.get("code") != code:
                    continue
                text = _clean_text(child.text)
                if text:
                    values.append(text)
            return values

        title = None
        title_fields = datafields("245")
        if title_fields:
            title = _join_title_parts(
                " ".join(subs(title_fields[0], "a")),
                " ".join(subs(title_fields[0], "b")),
            )

        publication_raw = self._first_subfield(datafields("269"), subs, "a")
        if not publication_raw:
            publication_raw = self._first_subfield(datafields("260"), subs, "c")

        authors = []
        for field in datafields("700"):
            authors.extend(subs(field, "a"))
        if not authors:
            for field in datafields("100"):
                authors.extend(subs(field, "a"))

        doi = None
        for field in datafields("024"):
            subfield_map = self._subfield_map(field)
            if any(value.lower() == "doi" for value in subfield_map.get("2", [])):
                doi = _normalise_doi((subfield_map.get("a") or [None])[0])
                if doi:
                    break

        abstract = self._first_subfield(datafields("520"), subs, "a")
        content_type = self._first_subfield(datafields("336"), subs, "a")
        native_type = self._first_subfield(datafields("037"), subs, "a")
        journal_raw = self._first_subfield(datafields("580"), subs, "a")
        journal, volume, issue = self._parse_journal_raw(journal_raw)
        series = self._first_subfield(datafields("490"), subs, "a")

        publisher = None
        publisher_field = datafields("260")
        if publisher_field:
            publisher = "; ".join(subs(publisher_field[0], "b")) or None

        departments = []
        department_ids = []
        for field in datafields("901"):
            departments.extend(subs(field, "u"))
            department_ids.extend(subs(field, "0"))

        geo_terms = []
        geo_ids = []
        for field in datafields("650"):
            geo_terms.extend(subs(field, "a"))
            geo_ids.extend(subs(field, "0"))

        thematics = []
        thematic_ids = []
        for field in datafields("653"):
            thematics.extend(subs(field, "a"))
            thematic_ids.extend(subs(field, "0"))

        pdf_urls = []
        file_ids = []
        file_sizes = []
        for field in datafields("856"):
            for url in subs(field, "u"):
                if ".pdf" in urllib.parse.urlparse(url).path.lower():
                    pdf_urls.append(url)
            file_ids.extend(subs(field, "9"))
            file_sizes.extend(subs(field, "s"))

        oai_ids = []
        sets = []
        for field in datafields("909"):
            oai_ids.extend(subs(field, "o"))
            sets.extend(subs(field, "p"))

        record_id = (controls("001") or [None])[0]
        modified_raw = (controls("005") or [None])[0]
        pdf_url = pdf_urls[0] if pdf_urls else None

        return {
            "record_id": record_id,
            "record_modified_raw": modified_raw,
            "record_modified_date": _date_from_marc_005(modified_raw),
            "title": title,
            "abstract": abstract,
            "publication_date_raw": publication_raw,
            "published_date": _normalise_date(publication_raw),
            "authors": self._dedupe(authors),
            "doi": doi,
            "category": content_type,
            "native_type": native_type,
            "journal_raw": journal_raw,
            "journal": journal,
            "series": series,
            "volume": volume,
            "issue": issue,
            "publisher": publisher,
            "departments": self._dedupe(departments),
            "department_ids": self._dedupe(department_ids),
            "geographical_areas": self._dedupe(geo_terms),
            "geographical_area_ids": self._dedupe(geo_ids),
            "thematics": self._dedupe(thematics),
            "thematic_ids": self._dedupe(thematic_ids),
            "pdf_url": pdf_url,
            "pdf_urls": self._dedupe(pdf_urls),
            "file_ids": self._dedupe(file_ids),
            "file_sizes": self._dedupe(file_sizes),
            "oai_ids": self._dedupe(oai_ids),
            "sets": self._dedupe(sets),
        }

    @staticmethod
    def _xml_local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1] if "}" in tag else tag

    @staticmethod
    def _first_subfield(fields: list[Any], subs_func, code: str) -> str | None:
        for field in fields:
            values = subs_func(field, code)
            if values:
                return values[0]
        return None

    def _subfield_map(self, field) -> dict[str, list[str]]:
        values: dict[str, list[str]] = {}
        for child in field:
            if self._xml_local(child.tag) != "subfield":
                continue
            code = child.attrib.get("code") or ""
            text = _clean_text(child.text)
            if code and text:
                values.setdefault(code, []).append(text)
        return values

    @staticmethod
    def _dedupe(values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            text = _clean_text(value)
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
        return result

    @staticmethod
    def _parse_journal_raw(journal_raw: str | None) -> tuple[str | None, str | None, str | None]:
        raw = _clean_text(journal_raw)
        if not raw:
            return None, None, None

        journal = raw
        journal = re.sub(r"^In:\s*", "", journal, flags=re.I)
        if ". -" in journal:
            journal = journal.split(". -", 1)[0]
        elif " - " in journal:
            journal = journal.split(" - ", 1)[0]
        journal = journal.strip(" .") or None

        volume = None
        issue = None
        volume_match = re.search(r"\bVolume\s+([^,;\s()]+)", raw, flags=re.I)
        if volume_match:
            volume = volume_match.group(1).strip()
        issue_match = re.search(r"\b(?:no\.?|issue)\s+([^,;\s()]+)", raw, flags=re.I)
        if issue_match:
            issue = issue_match.group(1).strip()
        return journal, volume, issue

    # ------------------------------------------------------------------
    # Record assembly
    # ------------------------------------------------------------------

    def _build_paper(self, list_item: dict[str, Any], detail: dict[str, Any], marc: dict[str, Any]) -> dict[str, Any] | None:
        record_id = marc.get("record_id") or detail.get("record_id") or list_item.get("record_id")
        external_id = record_id or detail.get("node_id") or list_item.get("slug")
        if not external_id:
            return None

        title = (
            marc.get("title")
            or detail.get("title")
            or list_item.get("title")
            or "(untitled)"
        )
        abstract = marc.get("abstract") or detail.get("abstract") or ""
        abstract = _clean_text(abstract)

        authors = marc.get("authors") or detail.get("authors") or list_item.get("authors") or []
        departments = marc.get("departments") or []
        publisher = marc.get("publisher") or list_item.get("edition") or "Geneva Graduate Institute"

        published_date = (
            marc.get("published_date")
            or _normalise_date(detail.get("publication_date_raw"))
            or list_item.get("listed_date")
        )
        listed_date = list_item.get("listed_date") or published_date
        listed_raw = list_item.get("listed_date_raw")
        post_number = record_id if record_id and record_id.isdigit() else external_id

        pdf_url = marc.get("pdf_url")
        original_filename = _filename_from_url(pdf_url)
        keywords = self._dedupe((marc.get("thematics") or []) + (marc.get("geographical_areas") or []))
        category = marc.get("category") or list_item.get("category")

        metadata = {
            "posted_date": listed_raw or listed_date,
            "listed_date": listed_date,
            "listed_date_raw": listed_raw,
            "originalFilename": original_filename,
            "journal_raw": marc.get("journal_raw"),
            "series": marc.get("series"),
            "volume": marc.get("volume"),
            "issue": marc.get("issue"),
            "record_id": record_id,
            "node_id": detail.get("node_id"),
            "post_number": post_number,
            "slug": list_item.get("slug"),
            "repository_url": detail.get("repository_url") or (f"{_REPOSITORY_BASE}/record/{record_id}" if record_id else None),
            "repository_marc_url": marc.get("repository_marc_url"),
            "record_modified_raw": marc.get("record_modified_raw"),
            "record_modified_date": marc.get("record_modified_date"),
            "publication_date_raw": marc.get("publication_date_raw") or detail.get("publication_date_raw"),
            "native_type": marc.get("native_type"),
            "department_ids": marc.get("department_ids"),
            "departments": departments,
            "geographical_areas": marc.get("geographical_areas"),
            "geographical_area_ids": marc.get("geographical_area_ids"),
            "thematics": marc.get("thematics"),
            "thematic_ids": marc.get("thematic_ids"),
            "oai_ids": marc.get("oai_ids"),
            "sets": marc.get("sets"),
            "pdf_urls": marc.get("pdf_urls"),
            "file_ids": marc.get("file_ids"),
            "file_sizes": marc.get("file_sizes"),
            "list_item": list_item,
        }
        metadata = {k: v for k, v in metadata.items() if v not in (None, "", [], {})}

        return {
            "id": f"{self.site_id}:{external_id}",
            "site_id": self.site_id,
            "external_id": str(external_id),
            "post_number": str(post_number) if post_number else None,
            "title": _clean_text(title),
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": "; ".join(authors) if isinstance(authors, list) else _clean_text(authors),
            "publisher": publisher,
            "department": "; ".join(departments),
            "journal": marc.get("journal"),
            "url": list_item.get("url"),
            "pdf_url": pdf_url,
            "keywords": ", ".join(keywords),
            "category": category,
            "doi": marc.get("doi"),
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page_index = 0
        seen_urls: set[str] = set()
        started = time.monotonic()
        limit_or_inf = limit if limit is not None else "inf"

        while page_index < _MAX_PAGES:
            if limit is not None and saved >= limit:
                break
            if time.monotonic() - started > _CRAWL_BUDGET_SECS - 30:
                print(f"[{self.site_id}] approaching 25 minute wall-clock budget; exiting cleanly")
                break

            display_page = page_index + 1
            if display_page == 1 or display_page % 10 == 0:
                print(f"[{self.site_id}] page {display_page}: saved {saved}/{limit_or_inf}")

            raw = self._curl_get(self._list_url(page_index), context=f"list page {display_page}")
            if not raw:
                print(f"[{self.site_id}] empty list page {display_page}; stopping")
                break

            list_items, has_next = self._parse_list_page(raw)
            new_items = []
            for item in list_items:
                url = item.get("url")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                new_items.append(item)

            if not new_items:
                print(f"[{self.site_id}] page {display_page} returned 0 new records; stopping")
                break

            for idx, item in enumerate(new_items, start=1):
                if limit is not None and saved >= limit:
                    break
                if time.monotonic() - started > _CRAWL_BUDGET_SECS - 30:
                    print(f"[{self.site_id}] approaching 25 minute wall-clock budget; exiting cleanly")
                    return saved

                item_label = item.get("url") or f"page {display_page} item {idx}"
                try:
                    time.sleep(self._detail_delay)
                    detail: dict[str, Any] = {}
                    record_id = item.get("record_id")

                    if not record_id:
                        detail_raw = self._curl_get(item["url"], context=f"detail {item_label}")
                        if detail_raw:
                            detail = self._parse_institute_detail(detail_raw, item["url"])
                            record_id = detail.get("record_id")

                    marc = self._fetch_repository_marc(record_id) if record_id else {}

                    if not marc.get("abstract") or not marc.get("title"):
                        if not detail:
                            detail_raw = self._curl_get(item["url"], context=f"detail fallback {item_label}")
                            if detail_raw:
                                detail = self._parse_institute_detail(detail_raw, item["url"])
                        if detail.get("record_id") and not record_id:
                            record_id = detail["record_id"]
                            marc = self._fetch_repository_marc(record_id)

                    paper = self._build_paper(item, detail, marc)
                    if paper is None:
                        print(f"[{self.site_id}] item {item_label} skipped: missing native ID")
                        continue

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < _MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {paper.get('external_id')} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:80]}")
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break
            if not has_next:
                print(f"[{self.site_id}] no next page link after page {display_page}; stopping")
                break

            page_index += 1

        if page_index >= _MAX_PAGES:
            print(f"[{self.site_id}] reached safety cap of {_MAX_PAGES} pages; stopping")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
