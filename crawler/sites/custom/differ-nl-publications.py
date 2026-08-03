# -*- coding: utf-8 -*-
"""DIFFER thesis publications crawler.

Starting point:
https://www.differ.nl/publications?author=&year=&type=thesis&combine=

The DIFFER repository is a Drupal/BibCite HTML view. List pages are plain
HTML at /publications?...&page=N (zero-based Drupal pager), and detail pages
are /bibcite/reference/{id}. DIFFER detail pages often contain only metadata,
so this crawler follows the detail page's "URL" field to the linked Pure
record when available and uses that record for the real abstract and PDF URL.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from html import unescape
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from crawler.base_crawler import BaseCrawler


_START_URL = "https://www.differ.nl/publications?author=&year=&type=thesis&combine="
_PAGE_CAP = 200
_CRAWL_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_RETRY_WAITS = (1, 3, 9)

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
    "mei": "05",
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


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = unescape(value)
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip(" ,;\n\t")


def _parse_date(raw: str | None) -> str | None:
    if not raw:
        return None
    value = _clean_text(raw)
    if not value:
        return None

    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", value)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"

    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", value)
    if m:
        month = _MONTHS.get(m.group(2).lower())
        if month:
            return f"{m.group(3)}-{month}-{m.group(1).zfill(2)}"

    m = re.search(r"(\d{4})[-/](\d{1,2})", value)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-01"

    return value


def _absolute_url(base_url: str, href: str | None) -> str | None:
    if not href:
        return None
    return urljoin(base_url, href.strip())


def _extract_reference_id(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"/bibcite/reference/([^/?#]+)", url)
    if m:
        return m.group(1)
    return None


def _doi_from_url_or_text(value: str | None) -> str | None:
    if not value:
        return None
    text = _clean_text(value)
    m = re.search(r"(10\.\d{4,9}/[^\s\"'<>]+)", text, flags=re.I)
    if m:
        return m.group(1).rstrip(".,);")
    parsed = urlparse(text)
    if parsed.netloc.lower().endswith("doi.org") and parsed.path:
        return parsed.path.lstrip("/")
    return None


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urlparse(url).path
    tail = unquote(path.rstrip("/").split("/")[-1])
    if tail and "." in tail and len(tail) <= 240:
        return tail
    if tail and len(tail) <= 200:
        return f"{tail}.pdf"
    return None


def _dedupe_join(values: list[str], sep: str = "; ") -> str | None:
    seen: set[str] = set()
    out: list[str] = []
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


def _make_soup(raw: str):
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup

            return BeautifulSoup(raw or "", parser)
        except Exception as exc:
            print(f"[differ-nl-publications] BeautifulSoup parser {parser} failed: {exc}")
            continue
    return None


class DifferNlPublicationsCrawler(BaseCrawler):
    site_id = "differ-nl-publications"
    site_name = "Custom: differ-nl-publications"
    base_url = "https://www.differ.nl"

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, *, referer: str | None = None) -> str | None:
        headers = [
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "-H",
            "Accept-Language: en-US,en;q=0.9",
        ]
        if referer:
            headers.extend(["-H", f"Referer: {referer}"])

        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--max-time",
            "45",
            *headers,
            url,
        ]

        for attempt, wait in enumerate(_RETRY_WAITS, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=55,
                )
                if result.stdout:
                    return result.stdout.decode("utf-8", errors="replace")
                err = result.stderr.decode("utf-8", errors="replace").strip()
                print(
                    f"[{self.site_id}] empty response attempt "
                    f"{attempt}/3 for {url}: {err}"
                )
            except Exception as exc:
                print(f"[{self.site_id}] curl error attempt {attempt}/3 for {url}: {exc}")

            if attempt < 3:
                time.sleep(wait)

        print(f"[{self.site_id}] fetch failed after 3 attempts: {url}")
        return None

    # ------------------------------------------------------------------
    # List parsing
    # ------------------------------------------------------------------

    def _list_url(self, logical_page: int) -> str:
        page_index = logical_page - 1
        if page_index <= 0:
            return _START_URL
        return f"{_START_URL}&page={page_index}"

    def _fetch_list_page(self, logical_page: int) -> tuple[list[dict[str, Any]], bool]:
        url = self._list_url(logical_page)
        raw = self._curl_get(url, referer=_START_URL)
        if not raw:
            return [], False
        soup = _make_soup(raw)
        if soup is None:
            return self._parse_list_fallback(raw), False
        return self._parse_list_soup(soup)

    def _parse_list_soup(self, soup) -> tuple[list[dict[str, Any]], bool]:
        view = soup.select_one(".view-differ-repository") or soup
        items: list[dict[str, Any]] = []

        for row in view.select(".views-row"):
            link = row.select_one(".views-field-title a[href]") or row.select_one(
                'a[href*="/bibcite/reference/"]'
            )
            if not link:
                continue

            detail_url = _absolute_url(self.base_url, link.get("href"))
            title = _clean_text(link.get_text(" ", strip=True))
            external_id = _extract_reference_id(detail_url)
            post_number = external_id if external_id and external_id.isdigit() else external_id

            author_text = ""
            author_el = row.select_one(".views-field-author-target-id-1 .field-content")
            if author_el:
                author_text = _clean_text(author_el.get_text(" ", strip=True))

            work_raw = ""
            work_el = row.select_one(".views-field-bibcite-type-of-work .field-content")
            if work_el:
                work_raw = _clean_text(work_el.get_text(" ", strip=True))

            work_type = None
            raw_date = None
            m = re.search(r"([^()]+)\(([^)]+)\)", work_raw)
            if m:
                work_type = _clean_text(m.group(1))
                raw_date = _clean_text(m.group(2))
            elif work_raw:
                work_type = work_raw

            doi = None
            doi_link = row.select_one(".views-field-bibcite-doi a[href]")
            if doi_link:
                doi = _doi_from_url_or_text(doi_link.get("href")) or _doi_from_url_or_text(
                    doi_link.get_text(" ", strip=True)
                )

            label_el = row.select_one(".views-field-bibcite-label")
            label = _clean_text(label_el.get_text(" ", strip=True)) if label_el else None

            items.append(
                {
                    "detail_url": detail_url,
                    "external_id": external_id,
                    "post_number": post_number,
                    "title": title,
                    "authors": author_text,
                    "work_raw": work_raw,
                    "work_type": work_type,
                    "raw_date": raw_date,
                    "published_date": _parse_date(raw_date),
                    "doi": doi,
                    "label": label,
                }
            )

        has_next = bool(view.select_one('a[rel="next"]'))
        return items, has_next

    def _parse_list_fallback(self, raw: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        pattern = re.compile(
            r'<a href="(?P<href>/bibcite/reference/[^"]+)"[^>]*>(?P<title>.*?)</a>.*?'
            r"views-field-bibcite-type-of-work.*?\((?P<date>\d{4}/\d{1,2}/\d{1,2})\)",
            flags=re.I | re.S,
        )
        for match in pattern.finditer(raw):
            detail_url = _absolute_url(self.base_url, match.group("href"))
            external_id = _extract_reference_id(detail_url)
            items.append(
                {
                    "detail_url": detail_url,
                    "external_id": external_id,
                    "post_number": external_id,
                    "title": _clean_text(re.sub(r"<[^>]+>", " ", match.group("title"))),
                    "authors": None,
                    "work_raw": None,
                    "work_type": "PhD",
                    "raw_date": match.group("date"),
                    "published_date": _parse_date(match.group("date")),
                    "doi": None,
                    "label": None,
                }
            )
        return items

    # ------------------------------------------------------------------
    # Detail parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, url: str, list_item: dict[str, Any]) -> dict[str, Any] | None:
        raw = self._curl_get(url, referer=_START_URL)
        if not raw:
            return None
        soup = _make_soup(raw)
        if soup is None:
            return None

        title_el = soup.select_one("h1.title") or soup.select_one("h1")
        title = _clean_text(title_el.get_text(" ", strip=True)) if title_el else list_item.get("title")

        fields: dict[str, Any] = {}
        table = soup.select_one(".bibcite-reference table") or soup.select_one("table")
        if table:
            for row in table.find_all("tr"):
                cells = row.find_all("td", recursive=False)
                if len(cells) < 2:
                    continue
                label = _clean_text(cells[0].get_text(" ", strip=True))
                value_cell = cells[1]
                label_key = label.lower()
                value = _clean_text(value_cell.get_text(" ", strip=True))

                if label_key == "author":
                    authors = [
                        _clean_text(x.get_text(" ", strip=True))
                        for x in value_cell.select(".field__item")
                    ]
                    fields["authors"] = _dedupe_join(authors) or value
                elif label_key == "url":
                    a = value_cell.find("a", href=True)
                    fields["external_url"] = a.get("href").strip() if a else value
                elif "download citation" in label_key:
                    fields["citation_exports"] = [
                        _absolute_url(self.base_url, a.get("href"))
                        for a in value_cell.find_all("a", href=True)
                    ]
                elif label_key == "":
                    bib_type = value_cell.select_one(".bibcite-type")
                    if bib_type:
                        fields["category"] = _clean_text(bib_type.get_text(" ", strip=True))
                elif "date published" in label_key:
                    fields["published_date_raw"] = value
                    fields["published_date"] = _parse_date(value)
                elif "year" in label_key:
                    fields["year"] = value
                elif label_key == "abstract":
                    fields["detail_abstract"] = value
                elif label_key in ("degree", "volume"):
                    fields["volume"] = value
                    fields["degree"] = value
                elif "thesis type" in label_key:
                    fields["thesis_type"] = value
                elif label_key in ("university", "publisher"):
                    fields["publisher"] = value
                elif label_key in ("city", "place published", "place of publication"):
                    fields["place_published"] = value
                elif "isbn" in label_key:
                    fields["isbn"] = value
                elif label_key == "label":
                    fields["access_label"] = value
                elif label_key == "pid":
                    fields["pid"] = value
                elif "doi" in label_key:
                    a = value_cell.find("a", href=True)
                    fields["doi"] = _doi_from_url_or_text(a.get("href") if a else value) or value
                elif label:
                    fields[label_key.replace(" ", "_")] = value

        if not fields.get("doi"):
            doi_link = soup.find("a", href=re.compile(r"doi\.org/", re.I))
            if doi_link:
                fields["doi"] = _doi_from_url_or_text(doi_link.get("href"))

        citation_el = soup.select_one(".bibcite-citation")
        if citation_el:
            fields["citation"] = _clean_text(citation_el.get_text(" ", strip=True))

        node_id = None
        settings = soup.find("script", attrs={"data-drupal-selector": "drupal-settings-json"})
        if settings and settings.string:
            try:
                current_path = json.loads(settings.string).get("path", {}).get("currentPath")
                if current_path:
                    node_id = _extract_reference_id("/" + current_path)
            except Exception:
                node_id = None

        external = {}
        external_url = fields.get("external_url")
        if external_url and external_url.startswith(("http://", "https://")):
            external = self._parse_external_record(external_url) or {}

        abstract_candidates = [
            external.get("abstract"),
            fields.get("detail_abstract"),
            fields.get("citation"),
        ]
        abstract = ""
        for candidate in abstract_candidates:
            candidate = _clean_text(candidate)
            if len(candidate) > len(abstract):
                abstract = candidate

        pdf_url = external.get("pdf_url")
        original_filename = external.get("original_filename") or _filename_from_url(pdf_url)

        published_date = (
            fields.get("published_date")
            or external.get("published_date")
            or list_item.get("published_date")
        )
        listed_date = list_item.get("published_date") or published_date

        authors = external.get("authors") or fields.get("authors") or list_item.get("authors")
        publisher = external.get("publisher") or fields.get("publisher")
        department = external.get("department") or external.get("organisations")
        doi = fields.get("doi") or external.get("doi") or list_item.get("doi")

        reference_id = list_item.get("external_id") or _extract_reference_id(url)
        metadata = {
            "posted_date": list_item.get("raw_date") or fields.get("published_date_raw"),
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": fields.get("volume"),
            "issue": None,
            "node_id": node_id or reference_id,
            "reference_id": reference_id,
            "pid": fields.get("pid"),
            "list_item": list_item,
            "differ_fields": fields,
            "external_record": external,
            "external_url": external_url,
            "external_source": external.get("source"),
            "isbn": fields.get("isbn") or external.get("isbn"),
            "place_published": fields.get("place_published") or external.get("place_published"),
            "access_label": fields.get("access_label") or list_item.get("label"),
        }

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": reference_id,
            "post_number": list_item.get("post_number") or reference_id,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": authors,
            "publisher": publisher,
            "department": department,
            "journal": None,
            "url": url,
            "pdf_url": pdf_url,
            "keywords": external.get("keywords"),
            "category": fields.get("category") or "Thesis",
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _parse_external_record(self, url: str) -> dict[str, Any] | None:
        raw = self._curl_get(url, referer=self.base_url)
        if not raw:
            return None
        soup = _make_soup(raw)
        if soup is None:
            return None

        meta = self._meta_values(soup)
        abstract = ""
        abstract_el = soup.select_one(".rendering_researchoutput_abstractportal .textblock")
        if abstract_el:
            abstract = _clean_text(abstract_el.get_text(" ", strip=True))

        authors = _dedupe_join(meta.get("citation_author", []))
        if not authors:
            author_nodes = soup.select(
                ".rendering_researchoutput_associatespersonsclassifiedportal li"
            )
            authors = _dedupe_join([n.get_text(" ", strip=True) for n in author_nodes])

        organisations = _dedupe_join(
            [
                node.get_text(" ", strip=True)
                for node in soup.select(
                    ".rendering_researchoutput_associatesorganisationsportal li"
                )
            ]
        )
        institutions = _dedupe_join(meta.get("citation_author_institution", []))

        details = self._parse_external_details_table(soup)
        pdf_url = self._first(meta.get("citation_pdf_url"))
        if not pdf_url:
            doc_link = soup.select_one(".rendering_researchoutput_publicationaccessrenderer a.document-link[href]")
            if doc_link:
                pdf_url = _absolute_url(url, doc_link.get("href"))

        keywords = self._parse_external_keywords(soup)
        doi = self._first(meta.get("citation_doi")) or _doi_from_url_or_text(
            self._first(meta.get("citation_doi_url"))
        )

        return {
            "source": "pure",
            "url": url,
            "title": self._first(meta.get("citation_title")),
            "abstract": abstract,
            "authors": authors,
            "organisations": organisations,
            "institutions": institutions,
            "department": details.get("awarding_institution") or organisations or institutions,
            "publisher": details.get("publisher") or self._first(meta.get("citation_publisher")),
            "published_date": _parse_date(self._first(meta.get("citation_publication_date"))),
            "online_date": _parse_date(self._first(meta.get("citation_online_date"))),
            "pdf_url": pdf_url,
            "original_filename": _filename_from_url(pdf_url),
            "keywords": keywords,
            "doi": doi,
            "language": self._first(meta.get("citation_language")),
            "isbn": details.get("print_isbns") or self._first(meta.get("citation_isbn")),
            "place_published": details.get("place_of_publication"),
            "qualification": details.get("qualification"),
            "publication_status": details.get("publication_status"),
            "raw_meta": meta,
            "raw_details": details,
        }

    @staticmethod
    def _meta_values(soup) -> dict[str, list[str]]:
        values: dict[str, list[str]] = {}
        for node in soup.find_all("meta"):
            name = node.get("name") or node.get("property")
            content = node.get("content")
            if not name or content is None:
                continue
            values.setdefault(name, []).append(_clean_text(content))
        return values

    @staticmethod
    def _first(values: list[str] | None) -> str | None:
        if not values:
            return None
        for value in values:
            cleaned = _clean_text(value)
            if cleaned:
                return cleaned
        return None

    @staticmethod
    def _parse_external_details_table(soup) -> dict[str, str]:
        details: dict[str, str] = {}
        table = soup.select_one(".rendering_researchoutput_detailsportal table.properties")
        if not table:
            return details

        for row in table.find_all("tr"):
            th = row.find("th")
            td = row.find("td")
            if not th or not td:
                continue
            key = _clean_text(th.get_text(" ", strip=True)).lower().replace("/", " ")
            key = re.sub(r"[^a-z0-9]+", "_", key).strip("_")
            value = _clean_text(td.get_text(" ", strip=True))
            if key and value:
                details[key] = value
        return details

    @staticmethod
    def _parse_external_keywords(soup) -> str | None:
        values: list[str] = []
        for selector in (
            ".rendering_researchoutput_keywordgroupsportal li",
            ".keyword-group li",
            ".relations.keywords li",
            ".concept-badge span",
        ):
            for node in soup.select(selector):
                text = _clean_text(node.get_text(" ", strip=True))
                if text:
                    values.append(text)
        return _dedupe_join(values, sep=", ")

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls: set[str] = set()
        started = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

        while page <= _PAGE_CAP:
            if limit is not None and saved >= limit:
                break
            if time.time() - started >= _CRAWL_BUDGET_SECONDS - 30:
                print(f"[{self.site_id}] wall-clock budget nearly reached; stopping cleanly.")
                break
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")
            if page == _PAGE_CAP:
                print(f"[{self.site_id}] safety cap of {_PAGE_CAP} pages reached.")

            items, has_next = self._fetch_list_page(page)
            if not items:
                print(f"[{self.site_id}] page {page}: no records found. Done.")
                break

            new_items = []
            for item in items:
                detail_url = item.get("detail_url")
                if not detail_url:
                    continue
                if detail_url in seen_urls:
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

                    title = paper.get("title") or ""
                    abstract = paper.get("abstract") or ""
                    if not title:
                        print(f"[{self.site_id}] item {detail_url} skipped: missing title")
                        continue
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {detail_url} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_or_inf}: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    item_name = detail_url or f"page {page} item {idx}"
                    print(f"[{self.site_id}] item {item_name} failed: {exc}")
                    continue

            if not has_next:
                print(f"[{self.site_id}] page {page}: next page link absent. Done.")
                break

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
