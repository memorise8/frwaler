# -*- coding: utf-8 -*-
"""ESRI publications crawler.

List endpoint:
    https://www.esri.ie/publications/browse?page=N

Detail endpoint:
    https://www.esri.ie/publications/<slug>

The site is Drupal 10. JSON:API is not publicly enabled, but rendered detail
pages expose rich citation_* and dcterms.* metadata.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - crawler runtime normally has bs4.
    BeautifulSoup = None


def _make_soup(raw: str):
    """Build BeautifulSoup with a defensive parser fallback chain."""
    if BeautifulSoup is None:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _clean_text(value) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value)).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _split_people(value: str) -> list[str]:
    value = _clean_text(value)
    if not value:
        return []
    if ";" in value:
        parts = value.split(";")
    elif "," in value and len(value) < 300:
        parts = value.split(",")
    else:
        parts = [value]
    return [_clean_text(p) for p in parts if _clean_text(p)]


def _split_keywords(value: str) -> list[str]:
    value = _clean_text(value)
    if not value:
        return []
    parts = re.split(r"\s*[;,]\s*", value)
    return [_clean_text(p) for p in parts if _clean_text(p)]


def _parse_date(raw: str | None) -> str | None:
    raw = _clean_text(raw)
    if not raw:
        return None

    match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", raw)
    if match:
        year, month, day = match.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"

    cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", raw, flags=re.I)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urlparse(url).path.rstrip("/")
    if not path:
        return None
    name = os.path.basename(path)
    return name[:200] if name else None


def _extract_post_number_from_id(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"teasertitle-(\d+)-", value)
    return match.group(1) if match else None


def _parse_journal_source(source: str | None) -> tuple[str | None, str | None, str | None]:
    source = _clean_text(source)
    if not source:
        return None, None, None
    journal = source.split(",", 1)[0].strip() or None

    volume = None
    match = re.search(r"\bVol\.?\s*([A-Za-z0-9.-]+)", source, flags=re.I)
    if match:
        volume = match.group(1)

    issue = None
    match = re.search(r"\b(?:Issue|No\.?)\s*([A-Za-z0-9.-]+)", source, flags=re.I)
    if match:
        issue = match.group(1)

    return journal, volume, issue


class EsriIePublicationsCrawler(BaseCrawler):
    site_id = "esri-ie-publications"
    site_name = "Custom: esri-ie-publications"
    base_url = "https://www.esri.ie"

    _START_URL = "https://www.esri.ie/publications/browse"
    _MAX_PAGES = 200
    _WALL_CLOCK_BUDGET_S = 25 * 60
    _MIN_ABSTRACT_CHARS = 50

    def _curl_get(self, url: str, accept: str = "text/html,application/xhtml+xml,*/*;q=0.9",
                  timeout: int = 30) -> str | None:
        """GET via curl with retry/backoff; decode with replacement on bad bytes."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", f"Accept: {accept}",
            url,
        ]
        for attempt, wait in enumerate((1, 3, 9), start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 10,
                )
                body = result.stdout.decode("utf-8", errors="replace")
                if body.strip():
                    return body
                err = result.stderr.decode("utf-8", errors="replace").strip()
                if err:
                    print(f"[{self.site_id}] empty response attempt {attempt}/3 for {url}: {err[:200]}")
            except Exception as exc:
                print(f"[{self.site_id}] curl error attempt {attempt}/3 for {url}: {exc}")
            if attempt < 3:
                time.sleep(wait)
        return None

    @staticmethod
    def _meta_all(soup, key: str) -> list[str]:
        values = []
        if not soup:
            return values
        for tag in soup.find_all("meta"):
            if tag.get("name") == key or tag.get("property") == key or tag.get("itemprop") == key:
                value = _clean_text(tag.get("content"))
                if value:
                    values.append(value)
        return values

    @classmethod
    def _meta_one(cls, soup, *keys: str) -> str | None:
        for key in keys:
            values = cls._meta_all(soup, key)
            if values:
                return values[0]
        return None

    def _extract_publication_details(self, soup) -> dict[str, str]:
        details: dict[str, str] = {}
        if not soup:
            return details
        for item in soup.select(".publication__details-item"):
            label = item.select_one(".publication__details-title")
            value = item.select_one(".publication__details-detail")
            label_text = _clean_text(label.get_text(" ", strip=True) if label else "")
            value_text = _clean_text(value.get_text(" ", strip=True) if value else "")
            if label_text:
                details[label_text] = value_text
        return details

    def _extract_drupal_settings(self, soup) -> dict:
        if not soup:
            return {}
        script = soup.find("script", attrs={"data-drupal-selector": "drupal-settings-json"})
        if not script:
            return {}
        raw = script.string or script.get_text()
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (TypeError, ValueError):
            return {}

    def _parse_list_page(self, raw: str) -> tuple[list[dict], bool]:
        soup = _make_soup(raw)
        if not soup:
            return [], False

        items = []
        for article in soup.select("article.teaser.node--type-publication"):
            link = article.select_one("h3.teaser__title a[href]")
            if not link:
                link = article.select_one('a[rel="bookmark"][href*="/publications/"]')
            if not link:
                continue

            href = link.get("href")
            url = urljoin(self.base_url, href)
            title = _clean_text(link.get_text(" ", strip=True))

            time_el = article.select_one("time[datetime]")
            listed_raw = None
            if time_el:
                listed_raw = time_el.get("datetime") or time_el.get_text(" ", strip=True)
            listed_date = _parse_date(listed_raw)

            h3 = article.select_one("h3.teaser__title")
            node_id = _extract_post_number_from_id(h3.get("id") if h3 else None)

            series_el = article.select_one(".teaser__esri-series")
            series = _clean_text(series_el.get_text(" ", strip=True) if series_el else "")

            authors = []
            authors_box = article.select_one(".teaser__meta-item--authors")
            if authors_box:
                for author_article in authors_box.select("article"):
                    text = _clean_text(author_article.get_text(" ", strip=True))
                    if text:
                        authors.append(text)

            research_areas = [
                _clean_text(a.get_text(" ", strip=True))
                for a in article.select(".teaser__meta-item--research-area a")
                if _clean_text(a.get_text(" ", strip=True))
            ]

            items.append({
                "url": url,
                "title": title,
                "listed_date": listed_date,
                "listed_date_raw": _clean_text(listed_raw),
                "node_id": node_id,
                "series": series or None,
                "authors": authors,
                "research_areas": research_areas,
                "category": ", ".join(research_areas) if research_areas else (series or None),
            })

        has_next = bool(soup.select_one('li.pager__item--next a[href], a[rel="next"][href]'))
        return items, has_next

    def _build_abstract(self, soup) -> str:
        abstract = self._meta_one(soup, "abstract", "description", "og:description", "twitter:description")
        if abstract:
            return abstract

        if not soup:
            return ""
        for selector in (
            ".publication__abstract",
            ".field--name-field-abstract",
            ".publication__body",
            ".field--name-body",
        ):
            node = soup.select_one(selector)
            text = _clean_text(node.get_text(" ", strip=True) if node else "")
            if len(text) >= self._MIN_ABSTRACT_CHARS:
                return text

        for paragraph in soup.find_all("p"):
            text = _clean_text(paragraph.get_text(" ", strip=True))
            if len(text) >= self._MIN_ABSTRACT_CHARS:
                return text
        return ""

    def _parse_detail(self, raw: str, list_item: dict) -> dict | None:
        soup = _make_soup(raw)
        if not soup:
            return None

        settings = self._extract_drupal_settings(soup)
        current_path = ((settings.get("path") or {}).get("currentPath") or "")
        node_match = re.search(r"node/(\d+)", current_path)
        node_id = node_match.group(1) if node_match else list_item.get("node_id")

        title = (
            self._meta_one(soup, "citation_title", "dcterms.title", "og:title")
            or list_item.get("title")
            or ""
        )
        title = _clean_text(title)
        if not title:
            return None

        detail_url = list_item["url"]
        slug = urlparse(detail_url).path.rstrip("/").split("/")[-1] or detail_url
        external_id = node_id or slug
        post_number = node_id or slug

        authors = self._meta_all(soup, "citation_author")
        if not authors:
            authors = _split_people(self._meta_one(soup, "dcterms.creator") or "")
        if not authors:
            authors = list_item.get("authors") or []

        publisher = (
            self._meta_one(soup, "citation_publisher", "dcterms.publisher")
            or self._extract_publication_details(soup).get("Publisher")
            or "ESRI"
        )
        publisher = _clean_text(publisher)

        published_raw = self._meta_one(
            soup,
            "citation_publication_date",
            "dcterms.date",
            "article:published_time",
        )
        published_date = _parse_date(published_raw) or list_item.get("listed_date")

        doi = self._meta_one(soup, "citation_doi", "dcterms.bibliographicCitation")
        doi = _clean_text(doi) or None

        pdf_url = self._meta_one(soup, "citation_pdf_url", "dcterms.identifier")
        if pdf_url:
            pdf_url = urljoin(self.base_url, pdf_url)
        original_filename = _filename_from_url(pdf_url)

        keyword_values = []
        for key in ("citation_keywords", "dcterms.subject", "keywords"):
            for value in self._meta_all(soup, key):
                keyword_values.extend(_split_keywords(value))
        seen_keywords = set()
        keywords = []
        for keyword in keyword_values:
            marker = keyword.lower()
            if marker not in seen_keywords:
                seen_keywords.add(marker)
                keywords.append(keyword)

        details = self._extract_publication_details(soup)
        publication_type = self._meta_one(soup, "dcterms.type") or details.get("ESRI Series") or list_item.get("series")
        series = details.get("ESRI Series") or list_item.get("series") or publication_type

        journal_raw = self._meta_one(soup, "dcterms.source")
        journal, volume, issue = _parse_journal_source(journal_raw)
        if publication_type and "journal" not in publication_type.lower():
            journal = None

        category = list_item.get("category") or publication_type or series
        abstract = self._build_abstract(soup)

        metadata = {
            "posted_date": list_item.get("listed_date_raw") or list_item.get("listed_date"),
            "listed_date": list_item.get("listed_date"),
            "originalFilename": original_filename,
            "journal_raw": journal_raw,
            "series": series,
            "volume": volume,
            "issue": issue,
            "node_id": node_id,
            "post_number": post_number,
            "slug": slug,
            "publication_type": publication_type,
            "research_areas": list_item.get("research_areas") or [],
            "list_title": list_item.get("title"),
            "list_series": list_item.get("series"),
            "list_url": list_item.get("url"),
            "published_date_raw": published_raw,
            "citation_pdf_url": pdf_url,
            "canonical_url": self._meta_one(soup, "canonical"),
            "article_published_time": self._meta_one(soup, "article:published_time"),
            "article_modified_time": self._meta_one(soup, "article:modified_time"),
            "publication_details": details,
            "detail_endpoint": detail_url,
            "list_endpoint": self._START_URL,
            "raw_meta": {
                "citation_title": self._meta_all(soup, "citation_title"),
                "citation_author": self._meta_all(soup, "citation_author"),
                "citation_doi": self._meta_all(soup, "citation_doi"),
                "citation_keywords": self._meta_all(soup, "citation_keywords"),
                "citation_publisher": self._meta_all(soup, "citation_publisher"),
                "citation_publication_date": self._meta_all(soup, "citation_publication_date"),
                "dcterms_type": self._meta_all(soup, "dcterms.type"),
                "dcterms_source": self._meta_all(soup, "dcterms.source"),
            },
        }

        return {
            "id": f"{self.site_id}:{external_id}",
            "site_id": self.site_id,
            "external_id": str(external_id) if external_id is not None else None,
            "post_number": str(post_number) if post_number is not None else None,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": list_item.get("listed_date"),
            "posted_date": list_item.get("listed_date"),
            "authors": "; ".join(authors) if authors else None,
            "publisher": publisher or None,
            "department": None,
            "journal": journal,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": ", ".join(keywords) if keywords else None,
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls: set[str] = set()
        started_at = time.monotonic()
        limit_or_inf = limit if limit is not None else "inf"

        while page <= self._MAX_PAGES:
            if limit is not None and saved >= limit:
                break
            if time.monotonic() - started_at >= self._WALL_CLOCK_BUDGET_S - 30:
                print(f"[{self.site_id}] approaching 25 minute wall-clock budget; stopping cleanly")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            list_url = self._START_URL if page == 1 else f"{self._START_URL}?page={page - 1}"
            raw = self._curl_get(list_url)
            if not raw:
                print(f"[{self.site_id}] page {page} failed or empty; stopping")
                break

            items, has_next = self._parse_list_page(raw)
            if not items:
                print(f"[{self.site_id}] page {page} returned 0 records; stopping")
                break

            new_items = []
            for item in items:
                url = item.get("url")
                if not url:
                    continue
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_items.append(item)

            if not new_items:
                print(f"[{self.site_id}] page {page} returned 0 new records; stopping")
                break

            for item in new_items:
                if limit is not None and saved >= limit:
                    break
                if time.monotonic() - started_at >= self._WALL_CLOCK_BUDGET_S - 30:
                    print(f"[{self.site_id}] approaching 25 minute wall-clock budget; stopping cleanly")
                    return saved

                item_url = item.get("url")
                try:
                    time.sleep(self._delay)
                    detail_raw = self._curl_get(item_url)
                    if not detail_raw:
                        print(f"[{self.site_id}] item {item_url} failed: empty detail response")
                        continue

                    paper = self._parse_detail(detail_raw, item)
                    if not paper:
                        print(f"[{self.site_id}] item {item_url} failed: could not parse detail")
                        continue

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self._MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] skip short abstract "
                            f"({len(abstract)} chars): {paper.get('title', '')[:80]}"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_url} failed: {exc}")
                    continue

            if not has_next:
                print(f"[{self.site_id}] no next page after page {page}; stopping")
                break

            page += 1

        if page > self._MAX_PAGES:
            print(f"[{self.site_id}] reached safety cap of {self._MAX_PAGES} pages")

        return saved
