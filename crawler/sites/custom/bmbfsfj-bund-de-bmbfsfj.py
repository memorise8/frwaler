# -*- coding: utf-8 -*-
"""Crawler for BMBFSFJ current announcements."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class BmbfsfjBundDeBmbfsfjCrawler(BaseCrawler):
    site_id = "bmbfsfj-bund-de-bmbfsfj"
    site_name = "Custom: bmbfsfj-bund-de-bmbfsfj"
    base_url = "https://www.bmbfsfj.bund.de"

    START_URL = "https://www.bmbfsfj.bund.de/bmbfsfj/aktuelles/alle-meldungen"
    DEFAULT_LIST_ENDPOINT = (
        "https://www.bmbfsfj.bund.de/bmbfsfj/aktuelles/alle-meldungen/72630!search"
    )
    BACKOFF_SECONDS = (1, 3, 9)
    CURL_TIMEOUT = 45
    MIN_ABSTRACT_CHARS = 50
    MAX_ABSTRACT_CHARS = 12000

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    def crawl(self, limit=None):
        """Crawl announcements from the CoreMedia HTML search endpoint."""
        saved = 0
        item_number = 0
        seen_external_ids = set()

        start_raw = self._curl_get_text(
            self.START_URL,
            context="start page",
            referer=self.base_url + "/bmbfsfj",
        )
        if not start_raw:
            print(f"[{self.site_id}] start page fetch failed")
            return saved

        start_soup = self._make_soup(start_raw, context="start page")
        if start_soup is None:
            print(f"[{self.site_id}] start page parse failed")
            return saved

        list_endpoint, dynamic_endpoint, state = self._discover_list_config(start_soup)
        print(
            f"[{self.site_id}] list endpoint: {list_endpoint}; "
            f"dynamic endpoint: {dynamic_endpoint or ''}"
        )

        page_num = 0
        while True:
            if limit is not None and saved >= limit:
                break

            if page_num == 0:
                page_raw = start_raw
                page_soup = start_soup
                page_url = self.START_URL
            else:
                if not state:
                    print(f"[{self.site_id}] no search state for page {page_num}; stopping")
                    break
                page_url = self._build_page_url(list_endpoint, state, page_num)
                page_raw = self._curl_get_text(
                    page_url,
                    context=f"list page {page_num + 1}",
                    referer=self.START_URL,
                )
                if not page_raw:
                    print(f"[{self.site_id}] list page {page_num + 1} fetch failed; stopping")
                    break
                page_soup = self._make_soup(page_raw, context=f"list page {page_num + 1}")
                if page_soup is None:
                    print(f"[{self.site_id}] list page {page_num + 1} parse failed; stopping")
                    break

            records = self._parse_list_records(page_soup)
            print(
                f"[{self.site_id}] list page {page_num + 1}: "
                f"discovered {len(records)} records"
            )
            if not records:
                break

            for record in records:
                if limit is not None and saved >= limit:
                    break

                item_number += 1
                try:
                    detail_url = record.get("url") or ""
                    if not detail_url:
                        raise RuntimeError("list record has no detail URL")

                    external_id = record.get("external_id") or self._external_id(detail_url)
                    if external_id in seen_external_ids:
                        continue
                    seen_external_ids.add(external_id)

                    time.sleep(self.detail_delay)
                    detail_raw = self._curl_get_text(
                        detail_url,
                        context=f"item {item_number} detail",
                        referer=page_url,
                    )
                    if not detail_raw:
                        raise RuntimeError("detail fetch failed after retries")

                    detail_soup = self._make_soup(
                        detail_raw,
                        context=f"item {item_number} detail",
                    )
                    if detail_soup is None:
                        raise RuntimeError("detail HTML could not be parsed")

                    parsed = self._parse_detail(detail_soup, record, detail_url)
                    title = parsed.get("title") or record.get("title") or ""
                    abstract = parsed.get("abstract") or record.get("abstract") or ""
                    abstract = self._clean_text(abstract)
                    if len(abstract) > self.MAX_ABSTRACT_CHARS:
                        abstract = abstract[: self.MAX_ABSTRACT_CHARS].rsplit(" ", 1)[0]

                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_number} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    category = (
                        parsed.get("category")
                        or record.get("category")
                        or "Aktuelle Meldung"
                    )
                    keywords = parsed.get("keywords") or []
                    if category and category not in keywords:
                        keywords.append(category)

                    metadata = {
                        "source": "CoreMedia HTML search/detail endpoints",
                        "start_url": self.START_URL,
                        "list_endpoint": list_endpoint,
                        "dynamic_endpoint": dynamic_endpoint,
                        "list_page_url": page_url,
                        "page_num": page_num,
                        "list_record": record,
                        "meta_description": parsed.get("meta_description") or "",
                        "image_url": parsed.get("image_url") or record.get("image_url") or "",
                        "pdf_links": parsed.get("pdf_links") or [],
                        "content_type": parsed.get("content_type") or "",
                    }

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "title": title,
                        "authors": json.dumps([], ensure_ascii=False),
                        "abstract": abstract,
                        "category": category,
                        "keywords": json.dumps(keywords, ensure_ascii=False),
                        "published_date": parsed.get("published_date") or "",
                        "url": detail_url,
                        "pdf_url": parsed.get("pdf_url") or "",
                        "doi": "",
                        "department": (
                            "Bundesministerium fuer Bildung, Familie, Senioren, "
                            "Frauen und Jugend (BMBFSFJ)"
                        ),
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {title[:90]}")
                except Exception as exc:
                    print(f"[bmbfsfj-bund-de-bmbfsfj] item {item_number} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break

            next_state = self._extract_search_state(page_soup)
            if next_state:
                state = next_state
            if not self._has_next_page(page_soup, page_num):
                break
            page_num += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    def _curl_get_text(self, url, *, context, referer=None):
        headers = [
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H",
            "Accept-Language: de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
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
            str(self.CURL_TIMEOUT),
            "-f",
            *headers,
            url,
        ]
        for attempt, wait in enumerate(self.BACKOFF_SECONDS, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=False,
                    timeout=self.CURL_TIMEOUT + 10,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout.decode("utf-8", errors="replace")
                message = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                if not message:
                    message = "empty response" if result.returncode == 0 else f"curl rc={result.returncode}"
                print(
                    f"[{self.site_id}] curl {context} failed "
                    f"(attempt {attempt}/3): {message}"
                )
            except subprocess.TimeoutExpired as exc:
                print(
                    f"[{self.site_id}] curl {context} timeout "
                    f"(attempt {attempt}/3): {exc}"
                )
            except Exception as exc:
                print(
                    f"[{self.site_id}] curl {context} error "
                    f"(attempt {attempt}/3): {exc}"
                )

            if attempt < len(self.BACKOFF_SECONDS):
                time.sleep(wait)

        return None

    def _make_soup(self, raw, *, context):
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup {parser} failed for {context}: {exc}")
        return None

    def _discover_list_config(self, soup):
        dynamic_endpoint = ""
        og_url = soup.select_one('meta[property="og:url"][content]')
        if og_url:
            content = (og_url.get("content") or "").strip()
            if "/dynamic/action/" in content:
                dynamic_endpoint = urljoin(self.base_url, content)

        form = soup.select_one("form#searchFormPaging[action]")
        if form is None:
            form = soup.select_one('form[action*="72630!search"]')

        list_endpoint = self.DEFAULT_LIST_ENDPOINT
        if form and form.get("action"):
            action = (form.get("action") or "").split("#", 1)[0]
            list_endpoint = urljoin(self.base_url, action)

        state = self._extract_search_state(soup)
        return list_endpoint, dynamic_endpoint, state

    def _extract_search_state(self, soup):
        if soup is None:
            return ""
        state_input = soup.select_one('form#searchFormPaging input[name="state"][value]')
        if state_input is None:
            state_input = soup.select_one('input[name="state"][value]')
        if state_input is None:
            return ""
        return state_input.get("value") or ""

    def _build_page_url(self, endpoint, state, page_num):
        query = urlencode({"state": state, "pageNum": str(page_num)})
        return f"{endpoint}?{query}#search72630"

    def _has_next_page(self, soup, current_page_num):
        next_button = soup.select_one('li.pager-item.next button[name="pageNum"]:not([disabled])')
        if next_button and self._safe_int(next_button.get("value"), -1) > current_page_num:
            return True

        for button in soup.select('button[name="pageNum"]'):
            value = self._safe_int(button.get("value"), -1)
            if value > current_page_num and button.get("disabled") is None:
                return True
        return False

    def _parse_list_records(self, soup):
        records = []
        for item in soup.select("li.teaser-list-item"):
            anchor = item.select_one("h3.teaser-headline a.text-link[href]")
            if anchor is None:
                anchor = item.select_one("a.text-link[href]")
            if anchor is None:
                continue

            href = (anchor.get("href") or "").strip()
            if not href:
                continue
            url = urljoin(self.base_url, href)
            if not self._is_bmbfsfj_url(url):
                continue

            title = self._clean_text(anchor.get_text(" ", strip=True))
            if not title:
                continue

            category_node = item.select_one(".teaser-tagline")
            abstract_node = item.select_one(".teaser-text")
            image_node = item.select_one("img[src]")

            record = {
                "external_id": self._external_id(url),
                "title": title,
                "abstract": self._clean_text(
                    abstract_node.get_text(" ", strip=True) if abstract_node else ""
                ),
                "category": self._clean_text(
                    category_node.get_text(" ", strip=True) if category_node else ""
                ),
                "url": url,
                "image_url": urljoin(self.base_url, image_node.get("src"))
                if image_node
                else "",
            }
            records.append(record)
        return records

    def _parse_detail(self, soup, record, detail_url):
        title = self._first_text(
            soup,
            [
                "h1.title .title__text",
                "h1.title",
                'meta[property="og:title"]',
                "title",
            ],
        )
        if title.endswith(" - BMBFSFJ"):
            title = title[: -len(" - BMBFSFJ")].strip()

        teaser = self._first_text(soup, [".article-intro .article-teaser", ".article-teaser"])
        content_node = soup.select_one(".article-content")
        body_text = ""
        if content_node is not None:
            content_copy = self._make_soup(
                str(content_node),
                context=f"detail content fragment {detail_url}",
            )
            if content_copy is not None:
                for unwanted in content_copy.select(
                    "script, style, noscript, figure, figcaption, .accordion-close"
                ):
                    unwanted.decompose()
                body_text = self._clean_text(content_copy.get_text(" ", strip=True))

        abstract_parts = []
        for part in (teaser, body_text, record.get("abstract") or ""):
            cleaned = self._clean_text(part)
            if cleaned and cleaned not in abstract_parts:
                abstract_parts.append(cleaned)
        abstract = "\n\n".join(abstract_parts)

        date_node = soup.select_one(".dateline time[datetime]")
        published_date = ""
        if date_node:
            published_date = (date_node.get("datetime") or "").strip()
        if not published_date:
            published_date = self._parse_german_date(
                self._first_text(soup, [".dateline time", ".dateline"])
            )

        category = self._first_text(
            soup,
            [
                ".article-intro .tagline",
                ".meta-tags .tagline",
                ".dateline",
            ],
        )
        content_type = ""
        if category and "Aktuelle Meldung" in category:
            content_type = "Aktuelle Meldung"
            category = category.replace("Aktuelle Meldung", "").strip()
        if not content_type:
            dateline = self._first_text(soup, [".dateline"])
            if "Aktuelle Meldung" in dateline:
                content_type = "Aktuelle Meldung"

        meta_description = self._meta_content(soup, 'meta[property="og:description"]')
        if not meta_description:
            meta_description = self._meta_content(soup, 'meta[name="description"]')

        image_url = self._meta_content(soup, 'meta[property="og:image"]')
        if not image_url:
            image_node = soup.select_one(".article-content img[src], .article-intro img[src]")
            if image_node:
                image_url = urljoin(self.base_url, image_node.get("src"))

        pdf_links = []
        for link in soup.select('a[href*=".pdf"], a[href*=".PDF"]'):
            href = link.get("href") or ""
            if not href:
                continue
            pdf_url = urljoin(self.base_url, href)
            label = self._clean_text(link.get_text(" ", strip=True))
            if not any(existing.get("url") == pdf_url for existing in pdf_links):
                pdf_links.append({"url": pdf_url, "label": label})

        keywords = []
        meta_keywords = self._meta_content(soup, 'meta[name="keywords"]')
        if meta_keywords:
            keywords.extend([k.strip() for k in meta_keywords.split(",") if k.strip()])
        if record.get("category") and record["category"] not in keywords:
            keywords.append(record["category"])
        if category and category not in keywords:
            keywords.append(category)

        return {
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "category": category or record.get("category") or content_type,
            "content_type": content_type,
            "keywords": keywords,
            "meta_description": meta_description,
            "image_url": image_url,
            "pdf_links": pdf_links,
            "pdf_url": pdf_links[0]["url"] if pdf_links else "",
            "detail_url": detail_url,
        }

    def _first_text(self, soup, selectors):
        for selector in selectors:
            node = soup.select_one(selector)
            if node is None:
                continue
            if node.name == "meta":
                value = node.get("content") or ""
            else:
                value = node.get_text(" ", strip=True)
            value = self._clean_text(value)
            if value:
                return value
        return ""

    def _meta_content(self, soup, selector):
        node = soup.select_one(selector)
        if node is None:
            return ""
        return self._clean_text(node.get("content") or "")

    def _external_id(self, url):
        path = urlparse(url).path
        match = re.search(r"-(\d+)(?:[./]?$|$)", path)
        if match:
            return match.group(1)
        digest = hashlib.sha1(url.encode("utf-8", errors="replace")).hexdigest()
        return digest[:20]

    def _is_bmbfsfj_url(self, url):
        parsed = urlparse(url)
        return parsed.netloc in {"www.bmbfsfj.bund.de", "bmbfsfj.bund.de"}

    def _parse_german_date(self, raw):
        match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", raw or "")
        if not match:
            return ""
        day, month, year = match.groups()
        return f"{year}-{month}-{day}"

    def _clean_text(self, value):
        if value is None:
            return ""
        text = unescape(str(value)).replace("\xa0", " ")
        return re.sub(r"\s+", " ", text).strip()

    def _safe_int(self, value, default=0):
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return default
