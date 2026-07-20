# -*- coding: utf-8 -*-
"""AMOLF publications crawler.

Starting URL: https://amolf.nl/publications

Discovery notes:
  - The custom WordPress type is ``aa_publications`` but it is not exposed
    through the public ``wp-json/wp/v2/types`` REST collection.
  - The real list endpoint is the rendered archive HTML:
    ``/publications`` and ``/publications/page/{N}``.
  - Details are publication permalink pages under ``/publications/{slug}``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from html import unescape
from urllib.parse import parse_qs, unquote, urljoin, urlparse

# Absolute import - spec_from_file_location has no package context.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from crawler.base_crawler import BaseCrawler


_START_URL = "https://amolf.nl/publications"
_PAGE_CAP = 200
_CRAWL_BUDGET_SECONDS = 25 * 60
_RETRY_WAITS = (1, 3, 9)

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = unescape(value)
    text = text.replace("\xa0", " ").replace("\u200b", "")
    text = text.replace("\u2010", "-").replace("\u2011", "-")
    return re.sub(r"\s+", " ", text).strip(" \t\r\n,;")


def _make_soup(raw: str):
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup

            return BeautifulSoup(raw or "", parser)
        except Exception as exc:
            print(f"[amolf-nl-publications] BeautifulSoup parser {parser} failed: {exc}")
            continue
    return None


def _parse_date(raw: str | None) -> str | None:
    value = _clean_text(raw)
    if not value:
        return None

    match = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", value)
    if match:
        return (
            f"{match.group(1)}-"
            f"{match.group(2).zfill(2)}-"
            f"{match.group(3).zfill(2)}"
        )

    match = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", value)
    if match:
        month = _MONTHS.get(match.group(2).lower())
        if month:
            return f"{match.group(3)}-{month:02d}-{int(match.group(1)):02d}"

    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value[:25], fmt).date().isoformat()
        except ValueError:
            pass

    return None


def _first_year(raw: str | None) -> str | None:
    match = re.search(r"\b(19|20)\d{2}\b", raw or "")
    return match.group(0) if match else None


def _slug_from_url(url: str | None) -> str | None:
    if not url:
        return None
    tail = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return unquote(tail) if tail else None


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None

    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    for key in ("att_id", "file", "filename"):
        value = qs.get(key)
        if value and value[0]:
            name = unquote(value[0].strip())
            if name:
                return name[:240]

    tail = unquote(parsed.path.rstrip("/").split("/")[-1])
    if tail and "." in tail:
        return tail[:240]
    return None


def _doi_from_text(value: str | None) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    match = re.search(r"(10\.\d{4,9}/[^\s\"'<>]+)", text, flags=re.I)
    if match:
        return match.group(1).rstrip(".,);")
    parsed = urlparse(text)
    if parsed.netloc.lower().endswith("doi.org") and parsed.path:
        return parsed.path.lstrip("/")
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


def _authors_from_reference(before_title: str | None) -> str | None:
    raw = _clean_text(before_title)
    if not raw:
        return None
    raw = re.sub(r"\s+\band\b\s+", ", ", raw)
    return _dedupe_join([part for part in re.split(r"\s*,\s*", raw) if part])


def _looks_like_thesis(reference_tail: str | None, doi: str | None = None) -> bool:
    tail = _clean_text(reference_tail).lower()
    if re.search(r"\b(university|universiteit|uva|tu delft|thesis)\b", tail):
        return True
    return bool(not doi and re.search(r",\s*\d{4}-\d{2}-\d{2}\s*$", tail))


def _parse_reference_parts(reference_text: str, title: str) -> dict:
    before = ""
    after = ""
    if title and title in reference_text:
        before, _, after = reference_text.partition(title)
    else:
        before = reference_text

    after = _clean_text(after)
    publisher = None
    journal = None
    category = "article"

    if _looks_like_thesis(after):
        category = "phdthesis"
        publisher = re.sub(r",?\s*\d{4}-\d{2}-\d{2}\s*$", "", after).strip(" ,") or None
    elif after:
        journal_base = re.sub(r"\([^)]*\d{4}[^)]*\)\s*$", "", after).strip(" ,")
        journal_base = re.sub(r"\b(19|20)\d{2}\b\s*$", "", journal_base).strip(" ,")
        journal_base = re.split(
            r"\s+\d+\b|,\s*\(|,\s*(?:[A-Za-z]?\d|e\d)",
            journal_base,
            maxsplit=1,
            flags=re.I,
        )[0]
        journal = _clean_text(journal_base) or None

    return {
        "authors": _authors_from_reference(before),
        "reference_tail": after or None,
        "publisher": publisher,
        "journal": journal,
        "category": category,
    }


def _extract_volume_issue(reference_html: str | None, reference_text: str | None) -> tuple[str | None, str | None]:
    volume = None
    issue = None

    soup = _make_soup(reference_html or "")
    if soup is not None:
        strong = soup.find("strong")
        if strong:
            volume = _clean_text(strong.get_text(" ", strip=True)) or None

    if not volume:
        match = re.search(r"\b(?:vol\.?\s*)?(\d{1,4})\b", reference_text or "", flags=re.I)
        if match:
            volume = match.group(1)

    match = re.search(r"\((\d{1,4})\)", reference_text or "")
    if match:
        issue = match.group(1)

    return volume, issue


class AmolfNlPublicationsCrawler(BaseCrawler):
    site_id = "amolf-nl-publications"
    site_name = "Custom: amolf-nl-publications"
    base_url = "https://amolf.nl"

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
                result = subprocess.run(cmd, capture_output=True, timeout=55)
                if result.stdout:
                    return result.stdout.decode("utf-8", errors="replace")
                err = result.stderr.decode("utf-8", errors="replace").strip()
                print(f"[{self.site_id}] empty response attempt {attempt}/3 for {url}: {err}")
            except Exception as exc:
                print(f"[{self.site_id}] curl error attempt {attempt}/3 for {url}: {exc}")

            if attempt < 3:
                time.sleep(wait)

        print(f"[{self.site_id}] fetch failed after 3 attempts: {url}")
        return None

    def _list_url(self, page: int) -> str:
        if page <= 1:
            return _START_URL
        return f"{self.base_url}/publications/page/{page}"

    def _fetch_list_page(self, page: int) -> tuple[list[dict], bool]:
        url = self._list_url(page)
        raw = self._curl_get(url, referer=_START_URL)
        if not raw:
            return [], False

        soup = _make_soup(raw)
        if soup is None:
            return self._parse_list_fallback(raw), False

        items: list[dict] = []
        for row in soup.select("li.list__item--publication"):
            meta = row.select_one(".publication__meta")
            link = row.select_one('a[href*="/publications/"]')
            meta_url_el = row.select_one('meta[itemprop="url"][content]')
            detail_url = None
            if meta_url_el:
                detail_url = urljoin(self.base_url, meta_url_el.get("content", ""))
            if not detail_url and link:
                detail_url = urljoin(self.base_url, link.get("href", ""))
            if not detail_url:
                continue

            title = ""
            if meta:
                em = meta.find("em")
                if em:
                    title = _clean_text(em.get_text(" ", strip=True))
            if not title and link:
                title = _clean_text(link.get_text(" ", strip=True))

            reference_text = _clean_text(meta.get_text(" ", strip=True)) if meta else ""
            parts = _parse_reference_parts(reference_text, title)
            raw_list_date = self._extract_list_date_raw(reference_text)
            list_date = _parse_date(raw_list_date)
            slug = _slug_from_url(detail_url)

            items.append(
                {
                    "detail_url": detail_url,
                    "title": title,
                    "slug": slug,
                    "reference": reference_text,
                    "authors": parts.get("authors"),
                    "publisher": parts.get("publisher"),
                    "journal": parts.get("journal"),
                    "category": parts.get("category"),
                    "reference_tail": parts.get("reference_tail"),
                    "raw_list_date": raw_list_date,
                    "listed_date": list_date,
                }
            )

        has_next = bool(soup.select_one("a.next.page-numbers"))
        return items, has_next

    def _parse_list_fallback(self, raw: str) -> list[dict]:
        items: list[dict] = []
        pattern = re.compile(
            r'<meta\s+itemprop=["\']url["\']\s+content=["\'](?P<url>[^"\']+)["\'][^>]*>'
            r".*?<em>(?P<title>.*?)</em>",
            flags=re.I | re.S,
        )
        for match in pattern.finditer(raw or ""):
            detail_url = urljoin(self.base_url, unescape(match.group("url")))
            title = _clean_text(re.sub(r"<[^>]+>", " ", match.group("title")))
            items.append(
                {
                    "detail_url": detail_url,
                    "title": title,
                    "slug": _slug_from_url(detail_url),
                    "reference": None,
                    "authors": None,
                    "publisher": None,
                    "journal": None,
                    "category": None,
                    "reference_tail": None,
                    "raw_list_date": None,
                    "listed_date": None,
                }
            )
        return items

    @staticmethod
    def _extract_list_date_raw(reference_text: str | None) -> str | None:
        text = reference_text or ""
        match = re.search(r"\b\d{4}-\d{2}-\d{2}\b", text)
        if match:
            return match.group(0)
        match = re.search(r"\((\d{4})\)\s*$", text)
        if match:
            return match.group(1)
        year = _first_year(text)
        return year

    def _parse_detail(self, url: str, list_item: dict) -> dict | None:
        raw = self._curl_get(url, referer=_START_URL)
        if not raw:
            return None
        soup = _make_soup(raw)
        if soup is None:
            return None

        title_el = soup.select_one(".article__title [itemprop='name']") or soup.select_one(
            ".article__title"
        )
        title = _clean_text(title_el.get_text(" ", strip=True)) if title_el else list_item.get("title")
        if not title:
            return None

        node_id = self._extract_node_id(soup, raw)
        external_id = node_id or list_item.get("slug") or _slug_from_url(url)
        post_number = external_id if external_id and str(external_id).isdigit() else list_item.get("slug")

        detail_fields, detail_field_html = self._parse_meta_table(soup)
        date_raw = detail_fields.get("Publication date")
        published_date = _parse_date(date_raw) or list_item.get("listed_date")
        listed_date = list_item.get("listed_date") or published_date

        doi = self._extract_doi(detail_fields, soup)
        reference_text = detail_fields.get("Reference") or list_item.get("reference") or ""
        reference_html = detail_field_html.get("Reference")
        ref_parts = _parse_reference_parts(reference_text, title)
        category = ref_parts.get("category") or list_item.get("category") or "publication"
        if doi and category == "phdthesis":
            category = "article"

        abstract = self._extract_abstract(soup)
        if len(abstract) < 50:
            print(f"[{self.site_id}] item {url} skipped: abstract too short ({len(abstract)} chars)")
            return None

        groups = self._extract_groups(soup)
        pdf_url, request_url = self._extract_pdf_and_request_urls(soup)
        original_filename = _filename_from_url(pdf_url)
        volume, issue = _extract_volume_issue(reference_html, reference_text)
        journal = ref_parts.get("journal") or list_item.get("journal")
        publisher = ref_parts.get("publisher") or list_item.get("publisher")
        authors = ref_parts.get("authors") or list_item.get("authors")

        canonical = self._canonical_url(soup) or url
        shortlink = self._shortlink(soup)
        yoast = self._yoast_dates(soup)
        slug = _slug_from_url(canonical)

        metadata = {
            "posted_date": list_item.get("raw_list_date") or date_raw,
            "listed_date": listed_date,
            "originalFilename": original_filename,
            "journal_raw": ref_parts.get("reference_tail") or list_item.get("reference_tail"),
            "series": None,
            "volume": volume,
            "issue": issue,
            "node_id": node_id,
            "post_id": node_id,
            "slug": slug,
            "shortlink": shortlink,
            "reference": reference_text,
            "reference_tail": ref_parts.get("reference_tail"),
            "list_reference": list_item.get("reference"),
            "list_item": list_item,
            "detail_fields": detail_fields,
            "yoast": yoast,
            "request_url": request_url,
            "source_list_endpoint": "/publications/page/{page}",
            "source_detail_endpoint": "/publications/{slug}",
        }

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": str(external_id) if external_id else canonical,
            "post_number": str(post_number) if post_number else None,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": authors,
            "publisher": publisher,
            "department": groups,
            "journal": journal,
            "url": canonical,
            "pdf_url": pdf_url,
            "keywords": None,
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    @staticmethod
    def _parse_meta_table(soup) -> tuple[dict[str, str], dict[str, str]]:
        fields: dict[str, str] = {}
        html: dict[str, str] = {}
        table = soup.select_one(".meta--publication table")
        if not table:
            return fields, html
        for row in table.find_all("tr"):
            th = row.find("th")
            td = row.find("td")
            if not th or not td:
                continue
            label = _clean_text(th.get_text(" ", strip=True)).replace("DOI", "DOI")
            value = _clean_text(td.get_text(" ", strip=True))
            if label and value:
                fields[label] = value
                html[label] = str(td)
        return fields, html

    @staticmethod
    def _extract_node_id(soup, raw: str) -> str | None:
        article = soup.select_one("article[id^='post--']")
        if article:
            match = re.search(r"post--(\d+)", article.get("id", ""))
            if match:
                return match.group(1)
        body = soup.find("body")
        if body:
            classes = " ".join(body.get("class", []))
            match = re.search(r"\bpostid-(\d+)\b", classes)
            if match:
                return match.group(1)
        match = re.search(r"shortlink'\s+href='https://amolf\.nl/\?p=(\d+)'", raw)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _extract_abstract(soup) -> str:
        section = soup.select_one("section.excerpt[itemprop='description']") or soup.select_one(
            "section.excerpt"
        )
        if not section:
            meta = soup.find("meta", attrs={"property": "og:description"})
            return _clean_text(meta.get("content")) if meta else ""
        paragraphs = [_clean_text(p.get_text(" ", strip=True)) for p in section.find_all("p")]
        paragraphs = [p for p in paragraphs if p]
        if paragraphs:
            return "\n\n".join(paragraphs)
        return _clean_text(section.get_text(" ", strip=True))

    @staticmethod
    def _extract_groups(soup) -> str | None:
        values: list[str] = []
        table = soup.select_one(".meta--publication table")
        if not table:
            return None
        for row in table.find_all("tr"):
            th = row.find("th")
            td = row.find("td")
            if not th or not td:
                continue
            label = _clean_text(th.get_text(" ", strip=True)).lower()
            if label not in ("group", "groups"):
                continue
            for link in td.find_all("a"):
                values.append(link.get_text(" ", strip=True))
            if not values:
                values.append(td.get_text(" ", strip=True))
        return _dedupe_join(values)

    @staticmethod
    def _extract_doi(fields: dict[str, str], soup) -> str | None:
        if fields.get("DOI"):
            return _doi_from_text(fields.get("DOI"))
        link = soup.find("a", href=re.compile(r"(doi\.org|dx\.doi\.org|10\.\d{4,9}/)", re.I))
        if link:
            return _doi_from_text(link.get("href")) or _doi_from_text(link.get_text(" ", strip=True))
        return None

    def _extract_pdf_and_request_urls(self, soup) -> tuple[str | None, str | None]:
        pdf_url = None
        request_url = None
        for link in soup.select(".cta--apply a[href], .aside--no-bullet a[href]"):
            href = urljoin(self.base_url, link.get("href", ""))
            label = _clean_text(link.get_text(" ", strip=True)).lower()
            if not href:
                continue
            if "download" in label or "request.pub.amolf.nl/request" in href:
                if not pdf_url:
                    pdf_url = href
            elif "request" in label or "/form/request" in href:
                if not request_url:
                    request_url = href
        return pdf_url, request_url

    @staticmethod
    def _canonical_url(soup) -> str | None:
        link = soup.find("link", attrs={"rel": "canonical"})
        href = link.get("href") if link else None
        return href.strip() if href else None

    @staticmethod
    def _shortlink(soup) -> str | None:
        link = soup.find("link", attrs={"rel": "shortlink"})
        href = link.get("href") if link else None
        return href.strip() if href else None

    @staticmethod
    def _yoast_dates(soup) -> dict:
        out: dict = {}
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string or script.get_text()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            graph = data.get("@graph") if isinstance(data, dict) else None
            if not isinstance(graph, list):
                continue
            for node in graph:
                if not isinstance(node, dict):
                    continue
                if node.get("datePublished"):
                    out["datePublished"] = node.get("datePublished")
                if node.get("dateModified"):
                    out["dateModified"] = node.get("dateModified")
        return out

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
            if page == _PAGE_CAP:
                print(f"[{self.site_id}] safety cap of {_PAGE_CAP} pages reached.")

            items, has_next = self._fetch_list_page(page)
            if not items:
                print(f"[{self.site_id}] page {page}: no records found. Done.")
                break

            new_items: list[dict] = []
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
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {detail_url} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue
                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_or_inf}: {paper.get('title', '')[:70]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    item_name = detail_url or f"page {page} item {idx}"
                    print(f"[{self.site_id}] item {item_name} failed: {exc}")
                    continue

            if page % 10 == 0:
                print(f"[amolf-nl-publications] page {page}: saved {saved}/{limit_or_inf}")

            if not has_next:
                print(f"[{self.site_id}] page {page}: next page link absent. Done.")
                break

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
