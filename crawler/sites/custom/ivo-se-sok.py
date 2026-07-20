# -*- coding: utf-8 -*-
"""Crawler for IVO.se publication search.

The public search page is server-rendered for the first page, but the real
"show more" endpoint used by the site is the same /sok/ route with AJAX
headers. It returns JSON with a HTML ``view`` fragment and ``meta`` pagination.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from urllib.parse import unquote, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class IvoSeSokCrawler(BaseCrawler):
    site_id = "ivo-se-sok"
    site_name = "Custom: ivo-se-sok"
    base_url = "https://www.ivo.se"

    START_URL = "https://www.ivo.se/sok/?q=%20&s=0&f=1"
    SEARCH_ENDPOINT = "https://www.ivo.se/sok/"
    MAX_PAGES = 200
    WALL_BUDGET_SECONDS = 25 * 60
    CURL_TIMEOUT = 45
    BACKOFF_SECONDS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 50
    PUBLISHER = "Inspektionen för vård och omsorg (IVO)"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        start_time = time.monotonic()
        limit_or_inf = limit if limit is not None else "inf"

        print(f"[ivo-se-sok] list endpoint: {self.SEARCH_ENDPOINT} AJAX JSON")
        print("[ivo-se-sok] detail endpoint: publication HTML pages linked from search results")

        for p in range(1, self.MAX_PAGES + 1):
            if limit is not None and saved >= limit:
                break
            if self._budget_exceeded(start_time):
                print(f"[ivo-se-sok] 25-minute budget reached before page {p}; stopping")
                break
            if p == 1 or p % 10 == 0:
                print(f"[ivo-se-sok] page {p}: saved {saved}/{limit_or_inf}")

            page_payload = self._fetch_search_page(p)
            if not page_payload:
                print(f"[ivo-se-sok] page {p}: empty response; stopping")
                break

            records = self._parse_search_records(page_payload["view"])
            meta = page_payload.get("meta") or {}
            if not records:
                print(f"[ivo-se-sok] page {p}: 0 new records from list endpoint; stopping")
                break

            page_had_new_url = False
            for item_index, record in enumerate(records, start=1):
                if limit is not None and saved >= limit:
                    break
                if self._budget_exceeded(start_time):
                    print(f"[ivo-se-sok] 25-minute budget reached during page {p}; stopping")
                    return saved

                item_url = record.get("url")
                if not item_url:
                    continue
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                page_had_new_url = True

                try:
                    time.sleep(self.detail_delay)
                    raw_detail = self._curl_text(
                        item_url,
                        context=f"item {p}-{item_index}",
                        referer=self.START_URL,
                    )
                    if not raw_detail:
                        print(f"[ivo-se-sok] item {item_url} failed: empty detail response")
                        continue

                    paper = self._build_paper(record, raw_detail, meta)
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[ivo-se-sok] item {paper.get('external_id') or item_url} "
                            f"skipped: abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[ivo-se-sok] item {record.get('external_id') or item_url} failed: {exc}")
                    continue

            if not page_had_new_url:
                print(f"[ivo-se-sok] page {p}: all URLs already seen; stopping")
                break

            total_pages = self._int_or_none(meta.get("totalPages"))
            if total_pages is not None and p >= total_pages:
                break

            if p == self.MAX_PAGES:
                print(f"[ivo-se-sok] safety cap of {self.MAX_PAGES} pages reached; stopping")

        return saved

    # ------------------------------------------------------------------
    # List endpoint
    # ------------------------------------------------------------------

    def _fetch_search_page(self, page):
        url = f"{self.SEARCH_ENDPOINT}?q=%20&s=0&f=1&p={page}"
        raw = self._curl_text(
            url,
            context=f"list page {page}",
            headers=[
                "X-Requested-With: XMLHttpRequest",
                "Accept: application/json, text/javascript, */*; q=0.01",
                f"Referer: {self.START_URL}",
            ],
        )
        if not raw:
            return None

        try:
            payload = json.loads(raw)
            view = payload.get("view") or ""
            if not isinstance(view, str):
                view = ""
            return {
                "view": view,
                "meta": payload.get("meta") if isinstance(payload.get("meta"), dict) else {},
            }
        except (TypeError, ValueError):
            # Defensive fallback for non-AJAX/server-rendered responses.
            soup = self._make_soup(raw, context=f"list page {page}")
            if soup is None:
                return None
            container = soup.select_one("#search-result") or soup
            return {"view": str(container), "meta": {}}

    def _parse_search_records(self, html_fragment):
        soup = self._make_soup(html_fragment, context="search result fragment")
        if soup is None:
            return []

        records = []
        for article in soup.select("a.search-article[href]"):
            url = self._clean_url(urljoin(self.base_url, article.get("href")))
            title = self._clean_text(
                self._text_of(article.select_one("h2")) or article.get("title") or ""
            )
            category = self._clean_text(self._text_of(article.select_one(".category")))
            date_raw = self._clean_text(self._text_of(article.select_one(".published-date")))
            listed_date = self._parse_iso_date(date_raw)
            excerpt = self._clean_text(self._text_of(article.select_one(".search-article__excerpt")))
            slug = self._slug_from_url(url)

            if not url or not title:
                continue

            records.append(
                {
                    "url": url,
                    "title": title,
                    "category": category or None,
                    "listed_date": listed_date,
                    "posted_date_raw": date_raw or None,
                    "excerpt": excerpt or None,
                    "slug": slug,
                    "external_id": slug,
                    "raw_href": article.get("href"),
                }
            )
        return records

    # ------------------------------------------------------------------
    # Detail parsing
    # ------------------------------------------------------------------

    def _build_paper(self, record, raw_detail, search_meta):
        soup = self._make_soup(raw_detail, context=f"detail {record.get('url')}")
        if soup is None:
            raise ValueError("detail HTML parse failed")

        canonical_url = self._canonical_url(soup, record["url"])
        title = (
            self._clean_text(self._text_of(soup.select_one(".hero__heading")))
            or self._meta_content(soup, "og:title", prop=True)
            or record.get("title")
        )

        article_meta = [self._clean_text(span.get_text(" ", strip=True)) for span in soup.select(".article-meta span")]
        category = record.get("category")
        if article_meta:
            category = article_meta[0] or category

        detail_date_raw = None
        if len(article_meta) >= 2:
            detail_date_raw = article_meta[1]
        side_meta = self._publication_meta(soup)
        published_date = (
            self._parse_iso_date(side_meta.get("Utgivningsdatum"))
            or self._parse_iso_date(detail_date_raw)
            or record.get("listed_date")
        )
        listed_date = record.get("listed_date") or published_date

        pdf_url = self._first_pdf_url(soup, canonical_url)
        original_filename = self._filename_from_url(pdf_url)

        ingress = self._clean_text(self._text_of(soup.select_one("p.ingress")))
        body = self._clean_text(self._text_of(soup.select_one(".mainbody.bodytext")))
        meta_description = self._meta_content(soup, "description")
        abstract = self._join_parts([ingress, body])
        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            abstract = self._join_parts([meta_description, record.get("excerpt")])

        diarienummer = side_meta.get("Diarienummer")
        publikationsnummer = side_meta.get("Publikationsnummer")
        post_number = publikationsnummer or self._numeric_token(diarienummer) or record.get("slug")
        external_id = diarienummer or publikationsnummer or record.get("slug") or canonical_url

        last_update_raw = self._clean_text(self._text_of(soup.select_one(".last-update")))
        last_updated = self._parse_iso_date(last_update_raw)

        metadata = {
            "posted_date": record.get("posted_date_raw") or listed_date,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "diarienummer": diarienummer,
            "publikationsnummer": publikationsnummer,
            "post_number": post_number,
            "slug": record.get("slug"),
            "canonical_url": canonical_url,
            "search_endpoint": self.SEARCH_ENDPOINT,
            "search_meta": search_meta,
            "list_record": record,
            "detail_article_meta": article_meta,
            "publication_meta": side_meta,
            "last_update_raw": last_update_raw or None,
            "last_updated": last_updated,
            "meta_description": meta_description,
        }

        return {
            "id": f"{self.site_id}:{external_id}",
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": None,
            "publisher": self.PUBLISHER,
            "department": None,
            "journal": None,
            "url": canonical_url,
            "pdf_url": pdf_url,
            "keywords": category,
            "category": category,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        }

    def _publication_meta(self, soup):
        data = {}
        for prop in soup.select(".publication-meta-property"):
            heading = self._clean_text(self._text_of(prop.select_one("h2")))
            if not heading:
                continue
            all_text = self._clean_text(prop.get_text(" ", strip=True))
            value = all_text
            if value.startswith(heading):
                value = value[len(heading):].strip(" :")
            if value:
                data[heading] = value
        return data

    def _first_pdf_url(self, soup, base):
        for link in soup.select("a[href]"):
            href = link.get("href") or ""
            if ".pdf" in href.lower():
                return self._clean_url(urljoin(base, href), keep_query=True)
        return None

    # ------------------------------------------------------------------
    # Network and parsing helpers
    # ------------------------------------------------------------------

    def _curl_text(self, url, *, context, headers=None, referer=None):
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
            "Accept-Language: sv-SE,sv;q=0.9,en;q=0.8",
        ]
        if headers:
            for header in headers:
                cmd.extend(["-H", header])
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        last_error = "unknown error"
        for attempt, wait in enumerate(self.BACKOFF_SECONDS, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.CURL_TIMEOUT + 10,
                    check=False,
                )
                text = (result.stdout or b"").decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                if result.returncode == 0 and text.strip():
                    return text
                last_error = stderr or f"curl exit {result.returncode}; empty body={not bool(text.strip())}"
            except subprocess.TimeoutExpired:
                last_error = "curl timeout"

            print(f"[ivo-se-sok] {context} fetch failed (attempt {attempt}/3): {last_error}")
            if attempt < len(self.BACKOFF_SECONDS):
                time.sleep(wait)

        print(f"[ivo-se-sok] {context} fetch failed after 3 attempts; skipping")
        return None

    def _make_soup(self, raw, *, context):
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                print(f"[ivo-se-sok] {context}: BeautifulSoup({parser}) failed: {exc}")
        return None

    # ------------------------------------------------------------------
    # Normalization helpers
    # ------------------------------------------------------------------

    def _canonical_url(self, soup, fallback):
        link = soup.find("link", rel=lambda value: value and "canonical" in value)
        href = link.get("href") if link else None
        return self._clean_url(urljoin(self.base_url, href or fallback))

    def _clean_url(self, url, *, keep_query=False):
        if not url:
            return None
        parsed = urlparse(urljoin(self.base_url, url))
        scheme = parsed.scheme or "https"
        netloc = parsed.netloc
        if netloc == "www.ivo.se:443":
            netloc = "www.ivo.se"
        query = parsed.query if keep_query else ""
        return urlunparse((scheme, netloc, parsed.path, "", query, ""))

    def _slug_from_url(self, url):
        path = urlparse(url or "").path.rstrip("/")
        if not path:
            return None
        return unquote(path.rsplit("/", 1)[-1]) or None

    def _filename_from_url(self, url):
        if not url:
            return None
        tail = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
        return tail if tail and "." in tail and len(tail) <= 240 else None

    def _meta_content(self, soup, name, *, prop=False):
        attrs = {"property": name} if prop else {"name": name}
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            return self._clean_text(tag.get("content"))
        return None

    def _parse_iso_date(self, text):
        if not text:
            return None
        match = re.search(r"\b(20\d{2}|19\d{2})-(\d{2})-(\d{2})\b", str(text))
        if match:
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        return None

    def _numeric_token(self, text):
        if not text:
            return None
        match = re.search(r"\b\d{4,}\b", str(text))
        return match.group(0) if match else None

    def _int_or_none(self, value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _budget_exceeded(self, start_time):
        return (time.monotonic() - start_time) >= self.WALL_BUDGET_SECONDS

    def _text_of(self, node):
        return node.get_text(" ", strip=True) if node is not None else ""

    def _clean_text(self, text):
        if text is None:
            return ""
        return re.sub(r"\s+", " ", str(text)).strip()

    def _join_parts(self, parts):
        cleaned = []
        for part in parts:
            text = self._clean_text(part)
            if text and text not in cleaned:
                cleaned.append(text)
        return " ".join(cleaned)
