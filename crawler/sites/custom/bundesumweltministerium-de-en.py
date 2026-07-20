# -*- coding: utf-8 -*-
"""Crawler for BMUKN English publications."""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class BundesumweltministeriumDeEnCrawler(BaseCrawler):
    site_id = "bundesumweltministerium-de-en"
    site_name = "Custom: bundesumweltministerium-de-en"
    base_url = "https://www.bundesumweltministerium.de"

    START_URL = (
        "https://www.bundesumweltministerium.de/en/public-information-service/"
        "publications"
    )
    ENGLISH_FILTER_NAME = "tx_bmubpublications_publications[filter][languageEn]"
    ENGLISH_FILTER_VALUE = "2"
    BACKOFF_SECONDS = (1, 3, 9)
    CURL_TIMEOUT = 60
    MIN_ABSTRACT_CHARS = 50
    PREFERRED_ABSTRACT_CHARS = 100
    CURL_META_MARKER = "__BMUKN_PUBLICATIONS_CURL_META__:"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    def crawl(self, limit=None):
        """Crawl English BMUKN publication records from TYPO3 HTML endpoints."""
        saved = 0
        item_number = 0
        seen_external_ids = set()

        start_raw, _ = self._curl_get_text(
            self.START_URL,
            context="start page",
            referer=self.base_url + "/en/",
        )
        if not start_raw:
            print(f"[{self.site_id}] start page fetch failed")
            return saved

        start_soup = self._make_soup(start_raw, context="start page")
        if start_soup is None:
            print(f"[{self.site_id}] start page could not be parsed")
            return saved

        english_filter = self._discover_english_filter(start_soup)
        page_url = self._build_list_url(1, english_filter)
        print(f"[{self.site_id}] list endpoint: {page_url} (TYPO3 HTML)")
        print(f"[{self.site_id}] detail endpoint pattern: /PU{{publication_id}}-1")

        while page_url:
            if limit is not None and saved >= limit:
                break

            list_raw, effective_list_url = self._curl_get_text(
                page_url,
                context="list page",
                referer=self.START_URL,
            )
            if not list_raw:
                print(f"[{self.site_id}] list page fetch failed: {page_url}")
                break

            list_soup = self._make_soup(list_raw, context="list page")
            if list_soup is None:
                print(f"[{self.site_id}] list page could not be parsed: {page_url}")
                break

            records = self._parse_list_records(list_soup, effective_list_url)
            print(f"[{self.site_id}] discovered {len(records)} records on list page")
            if not records:
                break

            for record in records:
                if limit is not None and saved >= limit:
                    break

                item_number += 1
                try:
                    if record.get("language") and record["language"].lower() != "english":
                        continue

                    external_id = record.get("external_id") or self._external_id(record)
                    if not external_id:
                        raise RuntimeError("record has no stable identifier")
                    if external_id in seen_external_ids:
                        continue
                    seen_external_ids.add(external_id)

                    detail_url = record.get("url") or ""
                    if not detail_url:
                        raise RuntimeError("record has no detail URL")

                    time.sleep(self.detail_delay)
                    detail_raw, effective_detail_url = self._curl_get_text(
                        detail_url,
                        context=f"item {item_number} detail",
                        referer=effective_list_url or page_url,
                    )
                    if not detail_raw:
                        raise RuntimeError("detail fetch failed after retries")

                    detail_soup = self._make_soup(
                        detail_raw,
                        context=f"item {item_number} detail",
                    )
                    if detail_soup is None:
                        raise RuntimeError("detail HTML could not be parsed")

                    detail = self._parse_detail(detail_soup, record, effective_detail_url)
                    abstract = detail.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_number} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue
                    if len(abstract) < self.PREFERRED_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_number} skipped: "
                            f"abstract below preferred length ({len(abstract)} chars)"
                        )
                        continue

                    category = detail.get("publication_type") or record.get("category") or ""
                    keywords = [
                        value
                        for value in [
                            category,
                            detail.get("language") or record.get("language"),
                            detail.get("publication_number") or record.get("publication_number"),
                        ]
                        if value
                    ]
                    metadata = {
                        "source": "TYPO3 BmubPublications HTML",
                        "start_url": self.START_URL,
                        "list_endpoint": effective_list_url or page_url,
                        "detail_short_url": detail_url,
                        "detail_url": effective_detail_url,
                        "list_record": record,
                        "facts": detail.get("facts") or {},
                        "date_modified": detail.get("date_modified") or "",
                        "image_url": detail.get("image_url") or record.get("image_url") or "",
                    }

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "title": detail.get("title") or record.get("title") or "",
                        "authors": json.dumps(["BMUKN"], ensure_ascii=False),
                        "abstract": abstract,
                        "category": category,
                        "keywords": json.dumps(keywords, ensure_ascii=False),
                        "published_date": detail.get("published_date") or "",
                        "url": effective_detail_url or detail_url,
                        "pdf_url": detail.get("pdf_url") or record.get("pdf_url") or "",
                        "doi": "",
                        "department": (
                            "Federal Ministry for the Environment, Climate Action, "
                            "Nature Conservation and Nuclear Safety (BMUKN)"
                        ),
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    if not paper["title"]:
                        raise RuntimeError("parsed record has no title")
                    if not paper["url"]:
                        raise RuntimeError("parsed record has no URL")

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {paper['title'][:90]}")
                except Exception as exc:
                    print(f"[bundesumweltministerium-de-en] item {item_number} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break
            page_url = self._next_page_url(list_soup)

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network and parsing helpers
    # ------------------------------------------------------------------

    def _curl_get_text(self, url, context="request", referer=None, accept=None):
        headers = [
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            f"Accept: {accept or 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'}",
            "-H",
            "Accept-Language: en-US,en;q=0.9,de;q=0.7",
        ]
        if referer:
            headers.extend(["-H", f"Referer: {referer}"])

        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--max-time",
            str(self.CURL_TIMEOUT),
            *headers,
            "-w",
            "\n" + self.CURL_META_MARKER + "%{http_code}\t%{url_effective}\t%{content_type}",
            url,
        ]

        last_error = ""
        for attempt, wait in enumerate(self.BACKOFF_SECONDS, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.CURL_TIMEOUT + 10,
                )
                text = result.stdout.decode("utf-8", errors="replace")
                body, meta = self._split_curl_meta(text)
                status_code, effective_url, _ = meta
                if result.returncode == 0 and 200 <= status_code < 400 and body.strip():
                    return body, effective_url or url

                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = (
                    f"curl exit={result.returncode} http={status_code} "
                    f"stderr={stderr[:200]}"
                )
            except subprocess.TimeoutExpired as exc:
                last_error = f"timeout after {exc.timeout}s"
            except Exception as exc:
                last_error = str(exc)

            if attempt < len(self.BACKOFF_SECONDS):
                print(
                    f"[{self.site_id}] {context} failed "
                    f"(attempt {attempt}/3): {last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] {context} failed after 3 attempts: {last_error}")
        return None, None

    def _split_curl_meta(self, text):
        if self.CURL_META_MARKER not in text:
            return text, (0, "", "")
        body, meta_raw = text.rsplit(self.CURL_META_MARKER, 1)
        parts = meta_raw.strip().split("\t", 2)
        while len(parts) < 3:
            parts.append("")
        try:
            status_code = int(parts[0])
        except (TypeError, ValueError):
            status_code = 0
        return body, (status_code, parts[1], parts[2])

    def _make_soup(self, raw, context="HTML"):
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                print(f"[{self.site_id}] {context} parse failed with {parser}: {exc}")
        return None

    def _discover_english_filter(self, soup):
        field = soup.select_one('input[name$="[languageEn]"]')
        if field and field.get("name"):
            return field["name"]
        return self.ENGLISH_FILTER_NAME

    def _build_list_url(self, page, english_filter):
        params = []
        if page and int(page) > 1:
            params.append(("tx_bmubpublications_publications[currentPage]", str(page)))
        if english_filter:
            params.append((english_filter, self.ENGLISH_FILTER_VALUE))
        if not params:
            return self.START_URL
        return f"{self.START_URL}?{urlencode(params)}"

    def _parse_list_records(self, soup, list_url):
        records = []
        for item in soup.select("article.c-publications-list__item"):
            title_link = item.select_one("h3.c-publications-list__title a")
            if title_link is None:
                title_link = item.select_one('a[href^="/PU"]')
            title = self._clean_text(title_link.get_text(" ", strip=True)) if title_link else ""
            detail_url = urljoin(self.base_url, title_link.get("href", "")) if title_link else ""

            pdf_link = item.select_one('a[download][href*=".pdf"], a[href*=".pdf"]')
            pdf_url = urljoin(self.base_url, pdf_link.get("href", "")) if pdf_link else ""

            image = item.select_one("img[data-src], img[src]")
            image_url = ""
            if image:
                image_url = urljoin(self.base_url, image.get("data-src") or image.get("src") or "")

            summary = item.select_one(".c-publications-list__content small")
            parts = self._split_pipe_text(summary.get_text(" ", strip=True) if summary else "")
            category = parts[0] if parts else ""
            publication_number = ""
            language = ""
            for part in parts[1:]:
                if part.lower().startswith("no."):
                    publication_number = part.replace("No.", "", 1).strip()
                elif part:
                    language = part.strip()

            item_id = (item.get("id") or "").strip()
            numeric_id = item_id.replace("publication-", "", 1) if item_id else ""
            records.append({
                "external_id": numeric_id or self._id_from_detail_url(detail_url),
                "list_item_id": item_id,
                "title": title,
                "url": detail_url,
                "pdf_url": pdf_url,
                "category": category,
                "publication_number": publication_number,
                "language": language,
                "image_url": image_url,
                "list_url": list_url,
            })
        return records

    def _parse_detail(self, soup, record, effective_url):
        article = soup.select_one("article#article") or soup
        title_node = article.select_one('h1[itemprop="headline"]') or article.select_one("h1")
        title = self._clean_text(title_node.get_text(" ", strip=True)) if title_node else record.get("title", "")

        published_date = ""
        date_node = article.select_one('meta[itemprop="datePublished"]')
        if date_node and date_node.get("content"):
            published_date = self._parse_date(date_node["content"])
        if not published_date:
            rubric = article.select_one(".c-hero__rubric")
            published_date = self._parse_date(rubric.get_text(" ", strip=True) if rubric else "")

        date_modified = ""
        modified_node = article.select_one('meta[itemprop="dateModified"]')
        if modified_node and modified_node.get("content"):
            date_modified = self._parse_date(modified_node["content"])

        pdf_node = article.select_one('a[download][href*=".pdf"], a[href*=".pdf"]')
        pdf_url = urljoin(self.base_url, pdf_node.get("href", "")) if pdf_node else record.get("pdf_url", "")

        image = article.select_one(".c-tile-image img[data-src], .c-tile-image img[src]")
        image_url = ""
        if image:
            image_url = urljoin(self.base_url, image.get("data-src") or image.get("src") or "")

        facts = self._parse_facts(article)
        abstract = self._extract_abstract(article)
        meta_description = self._meta_content(soup, "description")
        if len(abstract) < len(meta_description):
            abstract = meta_description

        publication_type = facts.get("Publication Type") or record.get("category") or ""
        language = facts.get("Language") or record.get("language") or ""
        publication_number = record.get("publication_number") or ""
        if not publication_number:
            publication_number = self._extract_publication_number(article)

        return {
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "date_modified": date_modified,
            "pdf_url": pdf_url,
            "publication_type": publication_type,
            "publication_number": publication_number,
            "language": language,
            "facts": facts,
            "image_url": image_url,
            "url": effective_url,
        }

    def _extract_abstract(self, article):
        candidates = []
        for formatted in article.select(".c-ce-formated"):
            parent = formatted.find_parent(class_="c-text")
            pieces = []
            if parent:
                heading = parent.find("h2", recursive=False)
                if heading:
                    pieces.append(self._clean_text(heading.get_text(" ", strip=True)))
            for node in formatted.find_all(["p", "li"], recursive=True):
                text = self._clean_text(node.get_text(" ", strip=True))
                if text:
                    pieces.append(text)
            text = self._clean_text(" ".join(pieces))
            if text:
                candidates.append(text)
        if candidates:
            return max(candidates, key=len)
        meta_description = self._meta_content(article, "description")
        return meta_description

    def _parse_facts(self, article):
        facts = {}
        for row in article.select(".c-meta-facts__row"):
            label_node = row.select_one(".c-meta-facts__label")
            if label_node is None:
                continue
            label = self._clean_text(label_node.get_text(" ", strip=True)).strip(": ")
            full_text = self._clean_text(row.get_text(" ", strip=True))
            value = re.sub(rf"^{re.escape(label)}\s*:?\s*", "", full_text).strip()
            if label and value:
                facts[label] = value
        return facts

    def _next_page_url(self, soup):
        next_link = soup.select_one("li.c-pagination__item--next a[href]")
        if next_link and next_link.get("href"):
            return urljoin(self.base_url, next_link["href"])
        return None

    @staticmethod
    def _clean_text(text):
        if not text:
            return ""
        return re.sub(r"\s+", " ", text).strip()

    def _split_pipe_text(self, text):
        text = self._clean_text(text)
        if not text:
            return []
        return [part.strip() for part in text.split("|") if part.strip()]

    def _meta_content(self, soup, name):
        node = soup.select_one(f'meta[name="{name}"]')
        if node and node.get("content"):
            return self._clean_text(node["content"])
        return ""

    @staticmethod
    def _parse_date(value):
        if not value:
            return ""
        value = value.strip()
        match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", value)
        if match:
            day, month, year = match.groups()
            return f"{year}-{month}-{day}"
        match = re.search(r"(\d{4}-\d{2}-\d{2})", value)
        if match:
            return match.group(1)
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            return ""

    @staticmethod
    def _id_from_detail_url(url):
        match = re.search(r"/PU(\d+)-", url or "")
        if match:
            return match.group(1)
        return ""

    def _extract_publication_number(self, article):
        rubric = article.select_one(".c-hero__rubric")
        if rubric:
            match = re.search(r"\bNo\.\s*([A-Za-z0-9._/-]+)", rubric.get_text(" ", strip=True))
            if match:
                return match.group(1)
        return ""

    def _external_id(self, record):
        if record.get("external_id"):
            return str(record["external_id"])
        if record.get("url"):
            return self._id_from_detail_url(record["url"])
        if record.get("pdf_url"):
            return record["pdf_url"].rstrip("/").split("/")[-1]
        return ""
