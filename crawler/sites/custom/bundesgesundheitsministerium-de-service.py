# -*- coding: utf-8 -*-
"""Crawler for Bundesgesundheitsministerium (BMG) — Gesundheit publications.

Target: https://www.bundesgesundheitsministerium.de/service/publikationen/gesundheit
Pagination: /seite-{n} path segments, 51+ pages.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_SITE_ID = "bundesgesundheitsministerium-de-service"
_BASE_URL = "https://www.bundesgesundheitsministerium.de"
_LIST_URL = f"{_BASE_URL}/service/publikationen/gesundheit"
_MAX_PAGES = 200
_ABSTRACT_MIN_CHARS = 100

_DE_MONTHS = {
    "januar": "01", "februar": "02", "märz": "03", "maerz": "03",
    "april": "04", "mai": "05", "juni": "06",
    "juli": "07", "august": "08", "september": "09",
    "oktober": "10", "november": "11", "dezember": "12",
}


def _parse_stand_date(raw: str) -> str:
    """Parse 'März 2025' or 'Stand: März 2025' into YYYY-MM-DD."""
    raw = re.sub(r"Stand:\s*", "", raw).strip()
    m = re.search(r"(\w+)\s+(\d{4})", raw)
    if not m:
        return ""
    month_word = m.group(1).lower()
    year = m.group(2)
    month = _DE_MONTHS.get(month_word, "")
    if not month:
        return ""
    return f"{year}-{month}-01"


class BundesgesundheitsministeriumDeServiceCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: bundesgesundheitsministerium-de-service"
    base_url = _BASE_URL

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self._detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url: str, *, referer: str | None = None, timeout: int = 45) -> str | None:
        cmd = [
            "curl",
            "--tls-max", "1.3",
            "-skL",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: de-DE,de;q=0.9,en;q=0.8",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        waits = [1, 3, 9]
        last_error = "unknown error"
        for attempt, wait in enumerate(waits, start=1):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 10, check=False,
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                raw = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and raw.strip():
                    return raw
                last_error = (
                    f"exit={result.returncode} "
                    f"stderr={result.stderr.decode('utf-8', errors='replace').strip()}"
                )
            if attempt < len(waits):
                print(
                    f"[{_SITE_ID}] curl attempt {attempt}/{len(waits)} failed for {url}: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{_SITE_ID}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    def _fetch_list_page(self, page: int) -> str | None:
        # Page 0 → base URL (same as seite-1); page n → /seite-{n+1}
        url = _LIST_URL if page == 0 else f"{_LIST_URL}/seite-{page + 1}"
        return self._curl(url, referer=_BASE_URL + "/")

    def _fetch_detail(self, url: str) -> str | None:
        time.sleep(self._detail_delay)
        return self._curl(url, referer=_LIST_URL)

    # ------------------------------------------------------------------
    # HTML helpers
    # ------------------------------------------------------------------

    def _parse_html(self, raw: str):
        text = raw or ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[{_SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
        return None

    @staticmethod
    def _clean(value) -> str:
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\xa0", " ").replace("​", "")
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        text = re.sub(r"\s+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def _one_line(cls, value) -> str:
        return re.sub(r"\s+", " ", cls._clean(value)).strip()

    @staticmethod
    def _slug_from_url(url: str) -> str:
        path = urlparse(url).path.strip("/")
        return path.rsplit("/", 1)[-1] if path else url

    # ------------------------------------------------------------------
    # List page parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, raw: str) -> list[dict]:
        soup = self._parse_html(raw)
        if soup is None:
            return []

        items = []
        seen_slugs: set[str] = set()

        # Walk every detail-page anchor; use h3 context to avoid c-block-link dupes
        for a in soup.find_all("a", href=re.compile(r"/service/publikationen/details/")):
            try:
                href = a.get("href", "").strip()
                if not href:
                    continue

                # Skip bare block links (prefer h3-anchored titles for richer context)
                if "c-block-link" in (a.get("class") or []):
                    continue

                url = urljoin(_BASE_URL, href)
                slug = self._slug_from_url(url)
                if slug in seen_slugs:
                    continue
                seen_slugs.add(slug)

                title = self._one_line(a.get_text(" ", strip=True))
                if not title:
                    title = a.get("title", "").strip()
                if not title:
                    continue

                # Walk up to find the nearest li/div ancestor that holds the full card
                card = a
                for _ in range(6):
                    parent = card.parent
                    if parent is None:
                        break
                    card = parent
                    if card.name in ("li", "article"):
                        break

                # Category (topic badge in c-teaser__content)
                category = ""
                cat_node = card.select_one(".c-teaser__content .c-category-title")
                if cat_node:
                    category = self._one_line(cat_node.get_text(" ", strip=True))

                # Download metadata spans
                pub_type = ""
                published_date = ""
                article_nr = ""
                pages_count = ""
                dl_data = card.select_one(".c-teaser__download__data")
                if dl_data:
                    for span in dl_data.select("span"):
                        text = self._one_line(span.get_text(" ", strip=True))
                        if "Artikel-Nr." in text:
                            article_nr = re.sub(r"Artikel-Nr\.\s*:?\s*", "", text).strip()
                        elif "Stand:" in text:
                            published_date = _parse_stand_date(text)
                        elif "Seiten:" in text:
                            pages_count = re.sub(r"Seiten:\s*", "", text).strip()
                        elif text and not pub_type:
                            # First non-empty span with no keyword → type label
                            pub_type = text

                # PDF link
                pdf_url = ""
                original_filename = ""
                pdf_node = card.select_one(".c-teaser__download__download-button a[href]")
                if pdf_node:
                    pdf_href = pdf_node.get("href", "").strip()
                    if pdf_href:
                        pdf_url = urljoin(_BASE_URL, pdf_href)
                        original_filename = pdf_href.rsplit("/", 1)[-1]

                items.append({
                    "url": url,
                    "slug": slug,
                    "title": title,
                    "category": category,
                    "pub_type": pub_type,
                    "published_date": published_date,
                    "article_nr": article_nr,
                    "pages_count": pages_count,
                    "pdf_url": pdf_url,
                    "original_filename": original_filename,
                })
            except Exception as exc:
                print(f"[{_SITE_ID}] list item parse error: {exc}")
                continue

        return items

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, raw: str, item: dict) -> dict:
        soup = self._parse_html(raw)
        if soup is None:
            raise ValueError("all HTML parsers failed on detail page")

        url = item["url"]
        slug = item["slug"]
        title = item["title"]

        # Refine title from og:title
        meta_title = soup.find("meta", attrs={"property": "og:title"})
        if meta_title and meta_title.get("content"):
            og_t = self._one_line(meta_title.get("content", ""))
            if og_t:
                title = og_t

        # Abstract
        abstract = self._extract_abstract(soup)

        # PDF from detail page (authoritative — may differ from list snippet)
        pdf_url = item.get("pdf_url", "")
        original_filename = item.get("original_filename", "")
        pdf_node = soup.select_one(".c-teaser__download__download-button a[href]")
        if pdf_node:
            pdf_href = pdf_node.get("href", "").strip()
            if pdf_href:
                pdf_url = urljoin(_BASE_URL, pdf_href)
                original_filename = pdf_href.rsplit("/", 1)[-1]

        # Metadata spans (refine from detail if list had blanks)
        published_date = item.get("published_date", "")
        article_nr = item.get("article_nr", "")
        pub_type = item.get("pub_type", "")
        pages_count = item.get("pages_count", "")
        dl_data = soup.select_one(".c-teaser__download__data")
        if dl_data:
            for span in dl_data.select("span"):
                text = self._one_line(span.get_text(" ", strip=True))
                if "Artikel-Nr." in text and not article_nr:
                    article_nr = re.sub(r"Artikel-Nr\.\s*:?\s*", "", text).strip()
                elif "Stand:" in text and not published_date:
                    published_date = _parse_stand_date(text)
                elif "Seiten:" in text and not pages_count:
                    pages_count = re.sub(r"Seiten:\s*", "", text).strip()
                elif text and not pub_type:
                    pub_type = text

        # Category badge on detail page
        category = item.get("category", "")
        cat_node = soup.select_one(".c-publication__download .c-category-title")
        if cat_node:
            cat_text = self._one_line(cat_node.get_text(" ", strip=True))
            if cat_text:
                category = cat_text

        # post_number: prefer article_nr (e.g. "BMG-G-12212"), else slug
        post_number = article_nr if article_nr else slug

        metadata = {
            "source": "BMG Gesundheit publications",
            "list_url": _LIST_URL,
            "canonical_url": url,
            "article_nr": article_nr,
            "pub_type": pub_type,
            "pages": pages_count,
            "originalFilename": original_filename,
        }

        return {
            "id": slug,
            "site_id": _SITE_ID,
            "external_id": slug,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "category": category,
            "keywords": "",
            "published_date": published_date,
            "listed_date": published_date,
            "url": url,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "publisher": "Bundesministerium für Gesundheit",
            "authors": "",
            "doi": "",
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _extract_abstract(self, soup) -> str:
        # 1. Primary: .c-publication__text paragraphs
        pub_text = soup.select_one(".c-publication__text")
        if pub_text:
            paras = []
            for p in pub_text.select("p"):
                text = self._one_line(p.get_text(" ", strip=True))
                if text and len(text) > 20:
                    paras.append(text)
            if paras:
                abstract = "\n\n".join(paras)
                if len(abstract) >= _ABSTRACT_MIN_CHARS:
                    return abstract

        # 2. og:description
        meta = soup.find("meta", attrs={"property": "og:description"})
        if meta and meta.get("content"):
            text = self._clean(meta.get("content", ""))
            if len(text) >= _ABSTRACT_MIN_CHARS:
                return text

        # 3. name=description
        meta2 = soup.find("meta", attrs={"name": "description"})
        if meta2 and meta2.get("content"):
            text = self._clean(meta2.get("content", ""))
            if len(text) >= _ABSTRACT_MIN_CHARS:
                return text

        # 4. Any paragraphs inside .c-publication
        pub = soup.select_one(".c-publication")
        if pub:
            paras = []
            for p in pub.select("p"):
                text = self._one_line(p.get_text(" ", strip=True))
                if text and len(text) > 20:
                    paras.append(text)
            if paras:
                abstract = "\n\n".join(paras)
                if len(abstract) >= _ABSTRACT_MIN_CHARS:
                    return abstract

        return ""

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        limit_label = str(limit) if limit is not None else "∞"
        crawl_start = time.time()

        for page in range(_MAX_PAGES):
            # 25-minute wall-clock budget
            if time.time() - crawl_start > 25 * 60:
                print(f"[{_SITE_ID}] 25-minute budget reached; stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page > 0 and page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_label}")

            try:
                raw = self._fetch_list_page(page)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] failed to fetch list page {page}: {exc}")
                break

            if not raw:
                print(f"[{_SITE_ID}] empty list response at page {page}; stopping")
                break

            items = self._parse_list_page(raw)
            if not items:
                print(f"[{_SITE_ID}] no items on page {page}; stopping")
                break

            # Detect silent wrap-around (paginator loops back to page 1)
            new_urls = [it["url"] for it in items if it["url"] not in seen_urls]
            if not new_urls:
                print(f"[{_SITE_ID}] all URLs on page {page} already seen; stopping")
                break

            if page == _MAX_PAGES - 1:
                print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached; stopping")

            for item in items:
                if limit is not None and saved >= limit:
                    break

                url = item["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                try:
                    raw_detail = self._fetch_detail(url)
                    if not raw_detail:
                        print(f"[{_SITE_ID}] item {url} failed: empty detail response")
                        continue

                    paper = self._parse_detail(raw_detail, item)

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < _ABSTRACT_MIN_CHARS:
                        print(
                            f"[{_SITE_ID}] item {url} skipped: abstract too short "
                            f"({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {url} failed: {exc}")
                    continue

        print(f"[{_SITE_ID}] crawl complete: saved {saved}/{limit_label}")
        return saved
