# -*- coding: utf-8 -*-
"""Institut Pasteur English press-release crawler."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class PasteurFrEnCrawler(BaseCrawler):
    site_id = "pasteur-fr-en"
    site_name = "Custom: pasteur-fr-en"
    base_url = "https://www.pasteur.fr"

    START_URL = "https://www.pasteur.fr/en/whats-new/press-area/press-releases-and-press-kits"
    BACKOFF_SECONDS = (1, 3, 9)
    CURL_TIMEOUT = 45
    MAX_PAGES = 200
    WALL_CLOCK_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    WALL_CLOCK_GRACE_SECONDS = 30
    MIN_ABSTRACT_CHARS = 50
    _CURL_META_MARKER = "__PASTEUR_FR_EN_CURL_META__:"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    def crawl(self, limit=None):
        """Crawl Pasteur's Drupal press-release list and article details."""
        saved = 0
        page = 0
        item_number = 0
        seen_urls = set()
        started_at = time.monotonic()
        limit_or_inf = str(limit) if limit is not None else "inf"

        while True:
            if limit is not None and saved >= limit:
                break
            if page >= self.MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {self.MAX_PAGES} pages reached")
                break
            if self._time_budget_exhausted(started_at):
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            list_url = self._list_url(page)
            raw, effective_list_url = self._curl_get_text(
                list_url,
                context=f"list page {page}",
                referer=self.START_URL,
            )
            if not raw:
                print(f"[{self.site_id}] list page {page} failed; stopping")
                break

            soup = self._make_soup(raw, context=f"list page {page}")
            if soup is None:
                print(f"[{self.site_id}] list page {page} could not be parsed; stopping")
                break

            records = self._parse_list(soup)
            if not records:
                print(f"[{self.site_id}] page {page} returned 0 records; stopping")
                break

            new_records = []
            for record in records:
                url = record.get("url")
                if not url:
                    continue
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_records.append(record)

            if not new_records:
                print(f"[{self.site_id}] page {page}: all records already seen; stopping")
                break

            for record in new_records:
                if limit is not None and saved >= limit:
                    break
                if self._time_budget_exhausted(started_at):
                    break

                item_number += 1
                try:
                    detail_url = record.get("url") or ""
                    if not detail_url:
                        raise RuntimeError("record has no detail URL")

                    time.sleep(self.detail_delay)
                    detail_raw, effective_detail_url = self._curl_get_text(
                        detail_url,
                        context=f"item {item_number} detail",
                        referer=effective_list_url or list_url,
                    )
                    if not detail_raw:
                        raise RuntimeError("detail fetch failed after retries")

                    detail_soup = self._make_soup(
                        detail_raw,
                        context=f"item {item_number} detail",
                    )
                    if detail_soup is None:
                        raise RuntimeError("detail HTML could not be parsed")

                    parsed = self._parse_detail(
                        detail_soup,
                        detail_raw,
                        record,
                        effective_detail_url or detail_url,
                    )
                    abstract = parsed.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_number} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    paper = self._paper_from_parsed(parsed)
                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[{self.site_id}] saved {saved}/{limit_or_inf}: "
                        f"{parsed.get('title', '')[:80]}"
                    )
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[pasteur-fr-en] item {item_number} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break
            if not self._has_next_page(soup):
                break
            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get_text(self, url, context="request", referer=None, accept=None):
        body, effective_url = self._curl_get_bytes(
            url,
            context=context,
            referer=referer,
            accept=accept
            or (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "application/json,*/*;q=0.8"
            ),
        )
        if body is None:
            return None, effective_url
        return body.decode("utf-8", errors="replace"), effective_url

    def _curl_get_bytes(self, url, context="request", referer=None, accept=None):
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
            "-A",
            self.USER_AGENT,
            "-H",
            f"Accept: {accept or '*/*'}",
            "-H",
            "Accept-Language: en-US,en;q=0.9,fr;q=0.7",
            "-w",
            "\n" + self._CURL_META_MARKER + "%{http_code}\t%{url_effective}",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.CURL_TIMEOUT + 10,
                )
                body, http_code, effective_url = self._split_curl_output(
                    result.stdout or b"",
                    url,
                )
                stderr = (result.stderr or b"").decode(
                    "utf-8",
                    errors="replace",
                ).strip()

                if result.returncode != 0:
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if http_code and http_code.isdigit() and int(http_code) >= 400:
                    raise RuntimeError(f"HTTP {http_code} for {effective_url}")
                if not body:
                    raise RuntimeError(f"empty response for {effective_url}")
                return body, effective_url
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                last_error = str(exc)
                print(
                    f"[{self.site_id}] {context} curl failed "
                    f"(attempt {attempt}/3): {last_error}"
                )
                if attempt < 3:
                    wait = self.BACKOFF_SECONDS[attempt - 1]
                    print(f"[{self.site_id}] retrying in {wait}s...")
                    time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None, url

    def _split_curl_output(self, raw, fallback_url):
        marker = ("\n" + self._CURL_META_MARKER).encode("ascii")
        marker_pos = raw.rfind(marker)
        if marker_pos == -1:
            return raw, "", fallback_url
        body = raw[:marker_pos]
        meta = raw[marker_pos + len(marker):].decode("utf-8", errors="replace").strip()
        if "\t" not in meta:
            return body, "", fallback_url
        http_code, effective_url = meta.split("\t", 1)
        return body, http_code.strip(), effective_url.strip() or fallback_url

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, raw, context="HTML"):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                last_exc = exc
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        print(f"[{self.site_id}] all BeautifulSoup parsers failed for {context}: {last_exc}")
        return None

    def _parse_list(self, soup):
        records = []
        for row in soup.select(".view-search-press .views-row, main article.teaser.-news"):
            title_link = row.select_one(".views-field-title a[href], .teaser__heading a[href]")
            if not title_link:
                title_link = row.select_one("a[href*='/press-documents/']")
            if not title_link:
                title_link = row.select_one("a[href]")
            if not title_link:
                continue

            href = title_link.get("href") or ""
            if not href:
                continue
            url = self._absolute_url(href)
            title = self._clean_text(title_link.get_text(" ", strip=True))
            if not title:
                continue

            date_el = row.select_one(".views-field-field-date .date-display-single, .teaser__meta")
            listed_date_raw = ""
            listed_date = ""
            if date_el:
                listed_date_raw = (
                    date_el.get("content")
                    or date_el.get_text(" ", strip=True)
                    or ""
                )
                listed_date = self._parse_date(listed_date_raw)

            body_el = row.select_one(".views-field-body .field-content")
            list_abstract = self._clean_text(body_el.get_text(" ", strip=True)) if body_el else ""

            category_el = row.select_one(".press_doc_type, .teaser__label")
            category = self._clean_text(category_el.get_text(" ", strip=True)) if category_el else ""

            image_el = row.select_one(".views-field-field-vignette img")
            image_url = self._absolute_url(image_el.get("src")) if image_el and image_el.get("src") else ""

            records.append(
                {
                    "title": title,
                    "url": url,
                    "listed_date": listed_date,
                    "listed_date_raw": listed_date_raw,
                    "list_abstract": list_abstract,
                    "category": category,
                    "image_url": image_url,
                }
            )
        return records

    def _parse_detail(self, soup, raw_html, record, effective_url):
        json_ld = self._extract_article_jsonld(soup)

        title = (
            self._first_text(soup, ["h1.node__title", "h1"])
            or self._meta_content(soup, "og:title")
            or self._clean_text(json_ld.get("name"))
            or record.get("title")
            or ""
        )
        title = self._clean_text(title)
        if not title:
            raise RuntimeError("detail title not found")

        article = soup.select_one("article[id^='node-']")
        node_id = self._extract_node_id(soup, article)
        detail_url = (
            self._meta_content(soup, "og:url")
            or self._clean_text(json_ld.get("url"))
            or effective_url
            or record.get("url")
            or ""
        )
        detail_url = self._absolute_url(detail_url)

        body_el = soup.select_one(".article__content .body") or soup.select_one("article .body")
        body_text = self._clean_text(body_el.get_text(" ", strip=True)) if body_el else ""
        if not body_text:
            # The redesigned page uses separate text sections, without article.
            sections = soup.select("main .paragraph--type--texte-reprise")
            body_text = self._clean_text(" ".join(section.get_text(" ", strip=True) for section in sections))
        json_body = self._clean_text(json_ld.get("articleBody"))
        og_description = self._clean_text(self._meta_content(soup, "og:description"))
        abstract = body_text or json_body or og_description or record.get("list_abstract") or ""
        abstract = self._clean_text(abstract)

        listed_date_raw = record.get("listed_date_raw") or ""
        listed_date = record.get("listed_date") or self._parse_date(listed_date_raw)
        detail_date_raw = self._detail_date_raw(soup, json_ld)
        detail_date = self._parse_date(detail_date_raw)
        created_raw = self._meta_content(soup, "article:published_time")
        published_date = self._parse_date(created_raw) or detail_date or listed_date

        category = (
            self._first_text(soup, [".field-pst-doc-type"])
            or record.get("category")
            or self._clean_text(json_ld.get("articleSection"))
            or "Press release"
        )
        category = self._clean_text(category)

        author_value = json_ld.get("author")
        publisher = self._publisher_from_jsonld(author_value) or "Institut Pasteur"
        doi = self._extract_doi(body_text or raw_html)
        source = self._extract_source_metadata(body_el)
        journal = source.get("journal") or ""
        authors = source.get("authors") or ""
        pdf_url = self._extract_pdf_url(soup)
        original_filename = self._filename_from_url(pdf_url)

        external_id = node_id or self._slug_from_url(detail_url)
        post_number = node_id or external_id

        image_url = (
            self._meta_content(soup, "og:image")
            or self._clean_text(json_ld.get("image"))
            or record.get("image_url")
            or ""
        )

        metadata = {
            "posted_date": listed_date_raw or listed_date,
            "listed_date": listed_date,
            "published_date_raw": created_raw or detail_date_raw,
            "detail_date_raw": detail_date_raw,
            "originalFilename": original_filename,
            "journal_raw": journal or None,
            "series": None,
            "volume": None,
            "issue": None,
            "node_id": node_id,
            "post_number": post_number,
            "slug": self._slug_from_url(detail_url),
            "category": category,
            "doi": doi,
            "source_title": source.get("title") or None,
            "source_url": source.get("url") or None,
            "article_published_time": created_raw or None,
            "article_modified_time": self._meta_content(soup, "article:modified_time") or None,
            "og_updated_time": self._meta_content(soup, "og:updated_time") or None,
            "image_url": image_url or None,
            "list_record": record,
            "json_ld": json_ld or None,
        }

        return {
            "id": f"{self.site_id}:{external_id}" if external_id else None,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "listed_date_raw": listed_date_raw,
            "authors": authors,
            "publisher": publisher,
            "department": "",
            "journal": journal,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": "",
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": metadata,
        }

    def _paper_from_parsed(self, parsed):
        return {
            "id": parsed.get("id"),
            "site_id": self.site_id,
            "external_id": parsed.get("external_id"),
            "post_number": parsed.get("post_number"),
            "title": parsed.get("title"),
            "abstract": parsed.get("abstract"),
            "published_date": parsed.get("published_date"),
            "listed_date": parsed.get("listed_date"),
            "posted_date": parsed.get("listed_date"),
            "authors": parsed.get("authors") or "",
            "publisher": parsed.get("publisher") or "",
            "department": parsed.get("department") or parsed.get("publisher") or "",
            "journal": parsed.get("journal") or "",
            "url": parsed.get("url"),
            "pdf_url": parsed.get("pdf_url"),
            "keywords": parsed.get("keywords") or "",
            "category": parsed.get("category") or "",
            "doi": parsed.get("doi") or "",
            "original_filename": parsed.get("original_filename"),
            "metadata": json.dumps(parsed.get("metadata") or {}, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Field extractors
    # ------------------------------------------------------------------

    def _extract_article_jsonld(self, soup):
        for script in soup.select('script[type="application/ld+json"]'):
            raw = script.string or script.get_text("", strip=True) or ""
            if not raw.strip():
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            candidates = data if isinstance(data, list) else [data]
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                type_value = candidate.get("@type")
                if type_value == "Article" or (
                    isinstance(type_value, list) and "Article" in type_value
                ):
                    return candidate
        return {}

    def _extract_node_id(self, soup, article):
        if article and article.get("id"):
            match = re.search(r"node-(\d+)", article.get("id") or "")
            if match:
                return match.group(1)
        body = soup.body
        if body:
            classes = " ".join(body.get("class") or [])
            match = re.search(r"page-node-(\d+)", classes)
            if match:
                return match.group(1)
        for link in soup.find_all("link", rel=True):
            rel = " ".join(link.get("rel") or [])
            href = link.get("href") or ""
            if "shortlink" in rel and "/node/" in href:
                match = re.search(r"/node/(\d+)", href)
                if match:
                    return match.group(1)
        return None

    def _extract_source_metadata(self, body_el):
        source = {"title": "", "url": "", "journal": "", "authors": ""}
        if body_el is None:
            return source

        paragraphs = body_el.find_all(["p", "div"], recursive=True)
        for idx, para in enumerate(paragraphs):
            para_text = self._clean_text(para.get_text(" ", strip=True))
            if not self._extract_doi(para_text):
                continue

            link = para.find("a", href=True)
            if link:
                source["title"] = self._clean_text(link.get_text(" ", strip=True))
                source["url"] = self._absolute_url(link.get("href") or "")

            journal_el = para.find("em")
            if journal_el:
                journal_text = self._clean_text(journal_el.get_text(" ", strip=True))
                if journal_text and journal_text.lower() != "source":
                    source["journal"] = journal_text

            if not source["journal"]:
                source["journal"] = self._journal_from_source_text(para_text)

            if idx + 1 < len(paragraphs):
                authors_text = self._clean_text(paragraphs[idx + 1].get_text(" ", strip=True))
                if self._looks_like_author_line(authors_text):
                    source["authors"] = self._split_authors(authors_text)
            break
        return source

    def _extract_doi(self, text):
        if not text:
            return ""
        match = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", text, re.IGNORECASE)
        if not match:
            return ""
        return match.group(0).rstrip(".,;:)")

    def _journal_from_source_text(self, text):
        if not text:
            return ""
        before_doi = re.split(r"\bDOI\b", text, flags=re.IGNORECASE)[0]
        parts = [p.strip() for p in before_doi.split(",") if p.strip()]
        if len(parts) >= 2:
            return parts[-1]
        return ""

    def _looks_like_author_line(self, text):
        if not text:
            return False
        lowered = text.lower()
        if "press contact" in lowered or "presse@" in lowered or "@" in text:
            return False
        if len(text) > 700:
            return False
        return bool(re.search(r"[A-Z]\.", text) or "," in text)

    def _split_authors(self, text):
        parts = re.split(r"\s*,\s*|\s+;\s*", text)
        authors = [self._clean_text(part) for part in parts if self._clean_text(part)]
        return "; ".join(authors)

    def _publisher_from_jsonld(self, author_value):
        if isinstance(author_value, dict):
            return self._clean_text(author_value.get("name"))
        if isinstance(author_value, list):
            names = []
            for value in author_value:
                if isinstance(value, dict):
                    name = self._clean_text(value.get("name"))
                else:
                    name = self._clean_text(value)
                if name:
                    names.append(name)
            return "; ".join(names)
        return self._clean_text(author_value)

    def _extract_pdf_url(self, soup):
        for link in soup.select("article a[href], .article__content a[href]"):
            href = link.get("href") or ""
            parsed_path = urlparse(href).path.lower()
            if parsed_path.endswith(".pdf"):
                return self._absolute_url(href)
        return None

    def _filename_from_url(self, url):
        if not url:
            return None
        tail = urlparse(url).path.rstrip("/").split("/")[-1]
        if not tail:
            return None
        return unquote(tail)

    # ------------------------------------------------------------------
    # General helpers
    # ------------------------------------------------------------------

    def _time_budget_exhausted(self, started_at):
        elapsed = time.monotonic() - started_at
        if elapsed >= self.WALL_CLOCK_BUDGET_SECONDS - self.WALL_CLOCK_GRACE_SECONDS:
            print(
                f"[{self.site_id}] approaching 25-minute wall-clock budget "
                f"({elapsed:.0f}s); exiting cleanly"
            )
            return True
        return False

    def _list_url(self, page):
        if page <= 0:
            return self.START_URL
        separator = "&" if "?" in self.START_URL else "?"
        return f"{self.START_URL}{separator}page={page}"

    def _has_next_page(self, soup):
        return soup.select_one("li.pager-next a[href], .pager__item--next a[href]") is not None

    def _absolute_url(self, href):
        href = (href or "").strip()
        if href.startswith("//"):
            return "https:" + href
        return urljoin(self.base_url, href)

    def _slug_from_url(self, url):
        path = urlparse(url or "").path.rstrip("/")
        slug = path.split("/")[-1] if path else ""
        return slug or None

    def _detail_date_raw(self, soup, json_ld):
        date_el = soup.select_one(".content__date .date-display-single, .hero__meta time")
        if date_el:
            value = date_el.get("content") or date_el.get_text(" ", strip=True)
            if value:
                return value
        if json_ld.get("datePublished"):
            return str(json_ld.get("datePublished"))
        value = self._meta_content(soup, "article:published_time")
        return value or ""

    def _meta_content(self, soup, property_name):
        tag = soup.find("meta", attrs={"property": property_name})
        if not tag:
            tag = soup.find("meta", attrs={"name": property_name})
        return tag.get("content", "").strip() if tag and tag.get("content") else ""

    def _first_text(self, soup, selectors):
        for selector in selectors:
            el = soup.select_one(selector)
            if not el:
                continue
            text = self._clean_text(el.get_text(" ", strip=True))
            if text:
                return text
        return ""

    def _parse_date(self, raw):
        if not raw:
            return ""
        text = self._clean_text(str(raw))
        # List cards append a reading-time label after a middle dot.
        human_date = text.split("·", 1)[0].strip()
        for fmt in ("%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(human_date, fmt).date().isoformat()
            except ValueError:
                pass
        match = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", text)
        if match:
            year, month, day = match.groups()
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        match = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", text)
        if match:
            day, month, year = match.groups()
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        return ""

    def _clean_text(self, value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()
