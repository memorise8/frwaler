# -*- coding: utf-8 -*-
"""Crawler for SCAHT English publications.

Discovery notes, verified with curl:
  - The requested historical URL ``/en/research/publications/`` currently
    returns a SCAHT 404 page.
  - The live English listing is the rendered HTML page
    ``/en/publications/`` with query-string pagination: ``?page=N``.
  - SCAHT detail pages expose date, title, author/publisher text, and a
    "Read more" outbound publication URL. Abstracts are usually available
    from that outbound page metadata or from Crossref for DOI-backed items.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
import uuid
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

# Absolute import - spec_from_file_location has no package context.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from crawler.base_crawler import BaseCrawler  # noqa: E402


_SITE_ID = "scaht-org-en"
_PARSERS = ("html5lib", "lxml", "html.parser")
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


def _make_soup(raw, context="html"):
    """Parse HTML defensively, preferring html5lib."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    for parser in _PARSERS:
        try:
            from bs4 import BeautifulSoup

            return BeautifulSoup(raw or "", parser)
        except Exception as exc:
            print(f"[{_SITE_ID}] BeautifulSoup({parser}) failed for {context}: {exc}")
            continue
    return None


def _clean_text(value):
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = text.replace("\xa0", " ").replace("\u200b", "")
    text = text.replace("\u2010", "-").replace("\u2011", "-")
    return re.sub(r"\s+", " ", text).strip(" \t\r\n")


def _clean_html_fragment(value):
    if not value:
        return ""
    soup = _make_soup(value, context="html fragment")
    if soup is None:
        return _clean_text(value)
    return _clean_text(soup.get_text(" ", strip=True))


def _parse_date(raw):
    value = _clean_text(raw)
    if not value:
        return None

    match = re.search(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b", value)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"

    match = re.search(r"\b(\d{4})[-/.](\d{1,2})\b", value)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}-01"

    for pattern in (
        r"\b(\d{1,2})\s+([A-Za-z]{3,12})\s+(\d{4})\b",
        r"\b([A-Za-z]{3,12})\s+(\d{1,2}),?\s+(\d{4})\b",
    ):
        match = re.search(pattern, value)
        if not match:
            continue
        if match.group(1).isdigit():
            day = int(match.group(1))
            month = _MONTHS.get(match.group(2).lower())
            year = int(match.group(3))
        else:
            month = _MONTHS.get(match.group(1).lower())
            day = int(match.group(2))
            year = int(match.group(3))
        if month:
            return f"{year:04d}-{month:02d}-{day:02d}"

    match = re.search(r"\b(19|20)\d{2}\b", value)
    if match:
        return f"{match.group(0)}-01-01"
    return None


def _date_from_parts(parts):
    if not parts:
        return None
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 1
        day = int(parts[2]) if len(parts) > 2 else 1
    except (TypeError, ValueError):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _join_semicolon(values):
    if not values:
        return None
    if isinstance(values, str):
        values = re.split(r"\s*;\s*", values)
    seen = set()
    out = []
    for value in values:
        cleaned = _clean_text(value).strip(" ,;")
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return "; ".join(out) if out else None


def _join_comma(values):
    if not values:
        return None
    if isinstance(values, str):
        values = re.split(r"\s*[,;]\s*", values)
    seen = set()
    out = []
    for value in values:
        cleaned = _clean_text(value).strip(" ,;")
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return ", ".join(out) if out else None


def _slug_from_url(url):
    path = urlparse(url or "").path.rstrip("/")
    slug = path.rsplit("/", 1)[-1] if path else ""
    return unquote(slug) or None


def _canonical_url(url):
    parsed = urlparse(url or "")
    path = parsed.path.rstrip("/") + "/"
    return parsed._replace(scheme="https", netloc="www.scaht.org", path=path, query="", fragment="").geturl()


def _filename_from_url(url):
    if not url:
        return None
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("filename", "file", "download", "attname"):
        if query.get(key) and query[key][0]:
            return unquote(query[key][0])[:240]

    tail = unquote(parsed.path.rstrip("/").split("/")[-1])
    if "/pdf/" in parsed.path and tail and not tail.lower().endswith(".pdf"):
        return f"{tail}.pdf"[:240]
    if tail and "." in tail:
        return tail[:240]
    return None


def _extract_doi(value):
    text = unquote(value or "")
    text = text.replace("\\/", "/")
    match = re.search(r"(10\.\d{4,9}/[^\s\"'<>]+)", text, flags=re.I)
    if not match:
        return None
    doi = match.group(1)
    doi = re.sub(r"^(doi:|https?://(?:dx\.)?doi\.org/)", "", doi, flags=re.I)
    doi = doi.split("?")[0].split("#")[0]
    doi = doi.rstrip(").,;]}\"'")
    for suffix in ("/full", "/abstract", "/pdf", "/xml"):
        if doi.lower().endswith(suffix):
            doi = doi[: -len(suffix)]
    return doi.lower() or None


def _extract_arxiv_id(value):
    parsed = urlparse(value or "")
    if "arxiv.org" not in parsed.netloc.lower():
        return None
    match = re.search(r"/(?:abs|pdf)/([^/?#]+)", parsed.path)
    if not match:
        return None
    return match.group(1).removesuffix(".pdf")


def _safe_json_dumps(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return json.dumps({"serialization_error": True}, ensure_ascii=False)


class SCAHTOrgEnCrawler(BaseCrawler):
    site_id = "scaht-org-en"
    site_name = "Custom: scaht-org-en"
    base_url = "https://www.scaht.org"

    START_URL = "https://www.scaht.org/en/research/publications/"
    LIVE_LIST_URL = "https://www.scaht.org/en/publications/"
    MAX_PAGES = 200
    WALL_BUDGET_SECONDS = 25 * 60
    WALL_MARGIN_SECONDS = 30
    MIN_ABSTRACT_CHARS = 100
    CURL_TIMEOUT = 45

    def crawl(self, limit=None):
        saved = 0
        page = 1
        start_time = time.monotonic()
        seen_urls = set()
        active_list_url = None
        limit_or_inf = limit if limit is not None else "inf"

        try:
            while page <= self.MAX_PAGES:
                if limit is not None and saved >= limit:
                    break
                if self._near_deadline(start_time):
                    print(f"[{self.site_id}] approaching 25 minute wall-clock budget; exiting cleanly")
                    break

                list_payload = self._fetch_list_page(page, active_list_url)
                if list_payload is None:
                    break
                active_list_url, list_url, records, next_url = list_payload
                if not records:
                    print(f"[{self.site_id}] page {page}: 0 records; stopping")
                    break

                new_on_page = 0
                for item_no, record in enumerate(records, start=1):
                    if limit is not None and saved >= limit:
                        break
                    if self._near_deadline(start_time):
                        print(f"[{self.site_id}] approaching 25 minute wall-clock budget; exiting cleanly")
                        return saved

                    detail_url = record.get("url")
                    dedupe_url = _canonical_url(detail_url)
                    if dedupe_url in seen_urls:
                        continue
                    seen_urls.add(dedupe_url)
                    new_on_page += 1

                    item_label = f"page {page} item {item_no} {dedupe_url}"
                    try:
                        time.sleep(self._delay)
                        paper = self._fetch_parse_save_item(record, item_label)
                        if not paper:
                            continue
                        abstract = paper.get("abstract") or ""
                        if len(abstract) < self.MIN_ABSTRACT_CHARS:
                            print(
                                f"[{self.site_id}] item {item_label} skipped: "
                                f"abstract too short ({len(abstract)} chars)"
                            )
                            continue
                        self._save_paper(paper)
                        saved += 1
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[{self.site_id}] item {item_label} failed: {exc}")
                        continue

                if page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

                if new_on_page == 0:
                    print(f"[{self.site_id}] page {page}: no new URLs; stopping")
                    break
                if not next_url:
                    break
                page += 1

            if page > self.MAX_PAGES:
                print(f"[{self.site_id}] reached safety cap of {self.MAX_PAGES} pages")
            return saved
        except KeyboardInterrupt:
            raise

    def _fetch_list_page(self, page, active_list_url):
        candidates = []
        if page == 1 and active_list_url is None:
            candidates = [self.START_URL, self.LIVE_LIST_URL]
        else:
            base = active_list_url or self.LIVE_LIST_URL
            candidates = [self._page_url(base, page)]

        for candidate in candidates:
            raw, effective_url, http_code, _ = self._curl_get(
                candidate,
                context=f"list page {page}",
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                referer=self.base_url + "/en/",
                retry_client_errors=False,
            )
            if not raw:
                if http_code and int(http_code) == 404 and candidate == self.START_URL:
                    print(f"[{self.site_id}] requested start URL returned 404; trying live /en/publications/")
                continue
            records, next_url = self._parse_list(raw, effective_url or candidate, page)
            if records:
                chosen = self.LIVE_LIST_URL if "/en/publications/" in (effective_url or candidate) else candidate
                return chosen, effective_url or candidate, records, next_url
            if http_code and int(http_code) == 404 and candidate == self.START_URL:
                print(f"[{self.site_id}] requested start URL returned 404; trying live /en/publications/")
        return None

    def _parse_list(self, raw, list_url, page):
        soup = _make_soup(raw, context=f"list page {page}")
        if soup is None:
            return [], None

        records = []
        for item in soup.select(".publications-item"):
            link = item.find("a", href=True)
            if not link:
                continue
            title = _clean_text(link.get_text(" ", strip=True))
            href = (link.get("href") or "").strip()
            if not title or not href:
                continue
            date_tag = item.select_one(".publications-date")
            listed_date_raw = _clean_text(date_tag.get_text(" ", strip=True)) if date_tag else ""
            detail_url = _canonical_url(urljoin(list_url, href))
            records.append(
                {
                    "title": title,
                    "url": detail_url,
                    "listed_date_raw": listed_date_raw,
                    "listed_date": _parse_date(listed_date_raw),
                    "slug": _slug_from_url(detail_url),
                    "list_page": page,
                    "list_url": list_url,
                }
            )

        next_url = None
        for link in soup.select("ul.pagination a.page-link[href]"):
            label = _clean_text(link.get_text(" ", strip=True)).lower()
            href = link.get("href") or ""
            if "»" in label or "next" in label:
                next_url = urljoin(list_url, href)
                break
        return records, next_url

    def _fetch_parse_save_item(self, record, item_label):
        detail = self._fetch_scaht_detail(record["url"], item_label)
        if not detail:
            return None

        source = self._fetch_source_metadata(detail, record, item_label)
        title = _clean_text(detail.get("title") or source.get("title") or record.get("title"))
        if not title:
            print(f"[{self.site_id}] item {item_label} skipped: missing title")
            return None

        abstract = _clean_text(source.get("abstract") or detail.get("abstract") or "")
        listed_date = record.get("listed_date")
        published_date = source.get("published_date") or detail.get("published_date") or listed_date
        slug = record.get("slug") or _slug_from_url(record.get("url"))
        if not slug:
            slug = hashlib.sha1(record["url"].encode("utf-8", errors="replace")).hexdigest()

        external_url = detail.get("external_url")
        doi = source.get("doi") or _extract_doi(external_url) or _extract_doi(record.get("url"))
        arxiv_id = source.get("arxiv_id") or _extract_arxiv_id(external_url)
        pdf_url = source.get("pdf_url")
        if not pdf_url and arxiv_id:
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
        original_filename = _filename_from_url(pdf_url)

        authors = source.get("authors") or detail.get("authors") or []
        publisher = source.get("publisher")
        journal = source.get("journal")
        keywords = source.get("keywords") or []
        category = source.get("category") or "publication"

        metadata = {
            "posted_date": record.get("listed_date_raw"),
            "posted_date_iso": listed_date,
            "listed_date": listed_date,
            "detail_date_raw": detail.get("published_date_raw"),
            "originalFilename": original_filename,
            "journal_raw": source.get("journal_raw") or journal,
            "series": source.get("series"),
            "volume": source.get("volume"),
            "issue": source.get("issue"),
            "slug": slug,
            "node_id": slug,
            "detail_url": record.get("url"),
            "external_url": external_url,
            "list_page": record.get("list_page"),
            "list_url": record.get("list_url"),
            "source": source.get("source"),
            "source_effective_url": source.get("effective_url"),
            "source_raw": source.get("raw"),
            "doi": doi,
            "arxiv_id": arxiv_id,
            "category": category,
        }

        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.site_id}:{slug}")),
            "site_id": self.site_id,
            "external_id": slug,
            "post_number": slug,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": _join_semicolon(authors),
            "publisher": _join_semicolon([publisher] if publisher else []),
            "department": "Swiss Centre for Applied Human Toxicology (SCAHT)",
            "journal": journal,
            "url": record.get("url"),
            "pdf_url": pdf_url,
            "keywords": _join_comma(keywords),
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": _safe_json_dumps(metadata),
        }

    def _fetch_scaht_detail(self, url, item_label):
        raw, effective_url, _, _ = self._curl_get(
            url,
            context=f"{item_label} detail",
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            referer=self.LIVE_LIST_URL,
        )
        if not raw:
            return {}
        soup = _make_soup(raw, context=f"{item_label} detail")
        if soup is None:
            return {}

        page = soup.select_one(".publication-page") or soup
        title = ""
        title_tag = page.select_one(".publication-title h1") or soup.select_one("h1")
        if title_tag:
            title = _clean_text(title_tag.get_text(" ", strip=True))

        raw_date = ""
        date_tag = page.select_one(".publication-date")
        if date_tag:
            raw_date = _clean_text(date_tag.get_text(" ", strip=True))

        authors = []
        for tag in page.select(".publication-authors p"):
            text = _clean_text(tag.get_text(" ", strip=True))
            if text:
                authors.extend([p.strip() for p in re.split(r"\s*;\s*", text) if p.strip()])

        external_url = None
        link = page.select_one(".publication-link a[href]")
        if link:
            external_url = urljoin(effective_url or url, link.get("href"))

        return {
            "title": title,
            "published_date_raw": raw_date,
            "published_date": _parse_date(raw_date),
            "authors": authors,
            "external_url": external_url,
            "effective_url": effective_url,
        }

    def _fetch_source_metadata(self, detail, record, item_label):
        external_url = detail.get("external_url")
        doi = _extract_doi(external_url or "")
        arxiv_id = _extract_arxiv_id(external_url or "")

        source = {}
        if doi:
            source = self._fetch_crossref(doi, item_label)
            if source.get("abstract"):
                return source

        if external_url:
            source = self._fetch_publisher_metadata(external_url, item_label)
            if source.get("abstract"):
                if doi and not source.get("doi"):
                    source["doi"] = doi
                if arxiv_id and not source.get("arxiv_id"):
                    source["arxiv_id"] = arxiv_id
                return source

        if doi and not source.get("abstract"):
            source = self._search_crossref(record.get("title"), doi, item_label)
            if source.get("abstract"):
                return source

        if source:
            return source
        return {"source": "scaht-detail-only", "doi": doi, "arxiv_id": arxiv_id}

    def _fetch_crossref(self, doi, item_label):
        url = f"https://api.crossref.org/works/{quote(doi, safe='')}"
        raw, effective_url, http_code, _ = self._curl_get(
            url,
            context=f"{item_label} crossref",
            accept="application/json,*/*",
            referer=self.LIVE_LIST_URL,
            retry_client_errors=False,
        )
        if not raw or (http_code and int(http_code) >= 400):
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] {item_label} crossref JSON parse failed: {exc}")
            return {}
        message = data.get("message") or {}
        return self._crossref_message_to_source(message, effective_url)

    def _search_crossref(self, title, doi, item_label):
        if not title:
            return {}
        url = "https://api.crossref.org/works?rows=1&query.title=" + quote(title)
        raw, effective_url, http_code, _ = self._curl_get(
            url,
            context=f"{item_label} crossref title search",
            accept="application/json,*/*",
            referer=self.LIVE_LIST_URL,
            retry_client_errors=False,
        )
        if not raw or (http_code and int(http_code) >= 400):
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        items = (((data.get("message") or {}).get("items")) or [])
        if not items:
            return {}
        message = items[0]
        if doi and (message.get("DOI") or "").lower() != doi.lower():
            return {}
        return self._crossref_message_to_source(message, effective_url)

    def _crossref_message_to_source(self, message, effective_url):
        authors = []
        for author in message.get("author") or []:
            if author.get("name"):
                name = author.get("name")
            else:
                name = " ".join(p for p in (author.get("given"), author.get("family")) if p)
            name = _clean_text(name)
            if name:
                authors.append(name)

        date_parts = (
            ((message.get("published") or {}).get("date-parts") or [])
            or ((message.get("published-online") or {}).get("date-parts") or [])
            or ((message.get("published-print") or {}).get("date-parts") or [])
            or ((message.get("issued") or {}).get("date-parts") or [])
        )
        published_date = _date_from_parts(date_parts[0]) if date_parts else None
        journal = self._first_value(message.get("container-title"))
        pdf_url = self._pdf_from_crossref_links(message.get("link") or [])
        abstract = _clean_html_fragment(message.get("abstract") or "")

        raw_subset = {
            "DOI": message.get("DOI"),
            "type": message.get("type"),
            "publisher": message.get("publisher"),
            "container-title": message.get("container-title"),
            "short-container-title": message.get("short-container-title"),
            "volume": message.get("volume"),
            "issue": message.get("issue"),
            "published": message.get("published"),
            "published-online": message.get("published-online"),
            "published-print": message.get("published-print"),
            "URL": message.get("URL"),
            "resource": message.get("resource"),
            "link": message.get("link"),
        }

        return {
            "source": "crossref-api",
            "effective_url": effective_url,
            "title": self._first_value(message.get("title")),
            "abstract": abstract,
            "authors": authors,
            "publisher": _clean_text(message.get("publisher") or "") or None,
            "journal": journal,
            "journal_raw": journal,
            "series": self._first_value(message.get("series-title")),
            "volume": message.get("volume"),
            "issue": message.get("issue"),
            "published_date": published_date,
            "doi": (message.get("DOI") or "").lower() or None,
            "pdf_url": pdf_url,
            "keywords": message.get("subject") or [],
            "category": message.get("type") or "publication",
            "raw": raw_subset,
        }

    def _fetch_publisher_metadata(self, url, item_label):
        raw, effective_url, http_code, content_type = self._curl_get(
            url,
            context=f"{item_label} source page",
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            referer=self.LIVE_LIST_URL,
            retry_client_errors=False,
        )
        if not raw or (http_code and int(http_code) >= 400):
            return {}

        soup = _make_soup(raw, context=f"{item_label} source page")
        if soup is None:
            return {}

        abstract = self._publisher_abstract(soup)
        title = self._meta_content(
            soup,
            ("citation_title", "dc.title", "DC.Title", "og:title", "twitter:title"),
        )
        if not title and soup.title:
            title = _clean_text(soup.title.get_text(" ", strip=True))

        authors = []
        for tag in soup.select("meta[name='citation_author'], meta[name='dc.creator'], meta[name='DC.Creator']"):
            value = _clean_text(tag.get("content") or "")
            if value:
                authors.append(value)

        keywords = []
        for tag in soup.select("meta[name='citation_keywords'], meta[name='keywords'], meta[name='dc.subject']"):
            value = _clean_text(tag.get("content") or "")
            if value:
                keywords.extend([p.strip() for p in re.split(r"\s*[,;]\s*", value) if p.strip()])

        published_date_raw = self._meta_content(
            soup,
            (
                "citation_publication_date",
                "citation_online_date",
                "article:published_time",
                "dc.date",
                "DC.Date",
            ),
        )
        journal = self._meta_content(
            soup,
            ("citation_journal_title", "citation_conference_title", "dc.source", "DC.Source"),
        )
        publisher = self._meta_content(soup, ("citation_publisher", "dc.publisher", "DC.Publisher"))
        doi = self._meta_content(soup, ("citation_doi", "dc.identifier", "DC.Identifier"))
        doi = _extract_doi(doi) or _extract_doi(effective_url or url) or _extract_doi(raw[:50000])
        pdf_url = self._meta_content(soup, ("citation_pdf_url",))
        if pdf_url:
            pdf_url = urljoin(effective_url or url, pdf_url)
        arxiv_id = _extract_arxiv_id(effective_url or url)

        raw_subset = {
            "content_type": content_type,
            "meta_description": self._meta_content(soup, ("description", "og:description")),
            "canonical": self._canonical_from_soup(soup, effective_url or url),
        }

        return {
            "source": "publisher-html",
            "effective_url": effective_url,
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "publisher": publisher or None,
            "journal": journal or None,
            "journal_raw": journal or None,
            "published_date": _parse_date(published_date_raw),
            "doi": doi,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": "publication",
            "arxiv_id": arxiv_id,
            "raw": raw_subset,
        }

    def _publisher_abstract(self, soup):
        for names in (
            ("citation_abstract",),
            ("dc.description", "DC.Description"),
            ("og:description", "twitter:description", "description"),
        ):
            value = self._meta_content(soup, names)
            if len(value) >= self.MIN_ABSTRACT_CHARS:
                return value

        for script in soup.select("script[type='application/ld+json']"):
            text = script.string or script.get_text("", strip=True)
            for value in self._json_ld_descriptions(text):
                if len(value) >= self.MIN_ABSTRACT_CHARS:
                    return value

        for selector in (
            "blockquote.abstract",
            "#Abs1-content",
            "section[data-title='Abstract']",
            "section[aria-labelledby^='Abs'] .c-article-section__content",
            ".c-article-section__content",
            ".abstract-content",
            "div.abstract",
            "#abstract",
            "[class*='abstract']",
            "[id*='abstract']",
        ):
            tag = soup.select_one(selector)
            if not tag:
                continue
            value = _clean_text(tag.get_text(" ", strip=True))
            value = re.sub(r"^abstract\s*:?\s*", "", value, flags=re.I)
            if len(value) >= self.MIN_ABSTRACT_CHARS:
                return value
        return ""

    def _json_ld_descriptions(self, text):
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return []
        values = []

        def walk(node):
            if isinstance(node, dict):
                for key in ("abstract", "description"):
                    value = _clean_html_fragment(node.get(key) or "")
                    if value:
                        values.append(value)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(data)
        return values

    def _meta_content(self, soup, names):
        for name in names:
            selectors = [f"meta[name='{name}']", f"meta[property='{name}']"]
            for selector in selectors:
                tag = soup.select_one(selector)
                if tag and tag.get("content"):
                    return _clean_text(tag.get("content"))
        return ""

    def _canonical_from_soup(self, soup, base):
        tag = soup.select_one("link[rel='canonical'][href]")
        if tag:
            return urljoin(base, tag.get("href"))
        return None

    def _pdf_from_crossref_links(self, links):
        for link in links:
            url = link.get("URL") or ""
            content_type = (link.get("content-type") or "").lower()
            if "pdf" in content_type or "/pdf" in url.lower() or url.lower().endswith(".pdf"):
                return url
        return None

    def _curl_get(
        self,
        url,
        context="request",
        accept="*/*",
        referer=None,
        timeout=None,
        retry_client_errors=True,
    ):
        timeout = timeout or self.CURL_TIMEOUT
        marker = "\n__SCAHT_CURL_META__"
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--max-time",
            str(timeout),
            "-A",
            self.USER_AGENT,
            "-H",
            f"Accept: {accept}",
            "-w",
            f"{marker}%{{http_code}}\t%{{url_effective}}\t%{{content_type}}",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        last_error = ""
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 15,
                    check=False,
                )
                stdout = result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                body, http_code, effective_url, content_type = self._split_curl_output(stdout, marker, url)
                status = int(http_code) if http_code.isdigit() else 0
                if result.returncode == 0 and 200 <= status < 400 and body.strip():
                    return body, effective_url, http_code, content_type

                last_error = stderr or f"curl exit {result.returncode}, http {http_code or 'unknown'}"
                if 400 <= status < 500 and not retry_client_errors:
                    print(f"[{self.site_id}] {context} returned HTTP {status} for {url}")
                    return "", effective_url, http_code, content_type
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                body = ""
                effective_url = url
                http_code = ""
                content_type = ""
                last_error = str(exc)

            if attempt < 2:
                wait = _RETRY_WAITS[attempt]
                print(
                    f"[{self.site_id}] {context} curl failed "
                    f"{attempt + 1}/3 for {url}: {last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts for {url}: {last_error}")
        return "", effective_url, http_code, content_type

    def _split_curl_output(self, raw, marker, fallback_url):
        if marker not in raw:
            return raw, "", fallback_url, ""
        body, meta = raw.rsplit(marker, 1)
        parts = meta.strip().split("\t", 2)
        http_code = parts[0] if parts else ""
        effective_url = parts[1] if len(parts) > 1 else fallback_url
        content_type = parts[2] if len(parts) > 2 else ""
        return body, http_code, effective_url, content_type

    def _page_url(self, base, page):
        if page <= 1:
            return base
        return urljoin(base, f"?page={page}")

    def _near_deadline(self, start_time):
        elapsed = time.monotonic() - start_time
        return elapsed >= self.WALL_BUDGET_SECONDS - self.WALL_MARGIN_SECONDS

    def _first_value(self, value):
        if isinstance(value, list):
            return _clean_text(value[0]) if value else None
        cleaned = _clean_text(value)
        return cleaned or None
