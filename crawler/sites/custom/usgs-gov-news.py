# -*- coding: utf-8 -*-
"""Crawler for USGS all news releases."""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime
from html import unescape
from urllib.parse import urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class UsgsGovNewsCrawler(BaseCrawler):
    site_id = "usgs-gov-news"
    site_name = "Custom: usgs-gov-news"
    base_url = "https://www.usgs.gov"

    _START_URL = "https://www.usgs.gov/news/news-releases/all-news-releases"
    _VIEWS_AJAX_URL = "https://www.usgs.gov/views/ajax"
    _PAGE_SIZE = 12
    _DEFAULT_VIEW = {
        "view_name": "hq_page",
        "view_display_id": "news_subtype",
        "view_args": "565736/news_subtype",
        "view_path": "/node/265968",
        "view_dom_id": "f408cf36fa357e13bd746797f3d65c9ead7242499f3cbd07d9947fcfd895ffb5",
        "pager_element": "0",
    }

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url, *, accept=None, referer=None, timeout=45):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--connect-timeout",
            "15",
            "--max-time",
            str(timeout),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            "Accept-Language: en-US,en;q=0.9",
        ]
        if accept:
            cmd.extend(["-H", f"Accept: {accept}"])
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        if "/views/ajax" in url:
            cmd.extend(["-H", "X-Requested-With: XMLHttpRequest"])
        cmd.append(url)

        waits = [1, 3, 9]
        last_error = "unknown error"
        for attempt, wait in enumerate(waits, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 10,
                    check=False,
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                raw = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and raw.strip():
                    return raw
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} stderr={stderr}"

            if attempt < len(waits):
                print(
                    f"[{self.site_id}] curl failed {attempt}/{len(waits)} for {url}: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    def _fetch_start_html(self):
        return self._curl(
            self._START_URL,
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            referer=self.base_url + "/",
        )

    def _fetch_detail_html(self, url):
        return self._curl(
            url,
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            referer=self._START_URL,
        )

    def _fetch_list_page_html(self, page, view_settings):
        params = {
            "_wrapper_format": "drupal_ajax",
            "view_name": view_settings.get("view_name") or self._DEFAULT_VIEW["view_name"],
            "view_display_id": (
                view_settings.get("view_display_id")
                or self._DEFAULT_VIEW["view_display_id"]
            ),
            "view_args": view_settings.get("view_args") or self._DEFAULT_VIEW["view_args"],
            "view_path": view_settings.get("view_path") or self._DEFAULT_VIEW["view_path"],
            "view_dom_id": (
                view_settings.get("view_dom_id") or self._DEFAULT_VIEW["view_dom_id"]
            ),
            "pager_element": (
                view_settings.get("pager_element") or self._DEFAULT_VIEW["pager_element"]
            ),
            "page": str(page),
        }
        url = f"{self._VIEWS_AJAX_URL}?{urlencode(params)}"
        raw = self._curl(
            url,
            accept="application/json,text/javascript,*/*;q=0.8",
            referer=self._START_URL,
        )
        if not raw:
            return self._fetch_static_list_page(page)

        try:
            commands = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] list JSON decode failed at page {page}: {exc}")
            return self._fetch_static_list_page(page)

        fragments = []
        if isinstance(commands, list):
            for command in commands:
                if not isinstance(command, dict):
                    continue
                if command.get("command") == "insert" and command.get("data"):
                    fragments.append(command.get("data") or "")
        html = "\n".join(fragments).strip()
        if html:
            return html
        print(f"[{self.site_id}] no list HTML in AJAX response at page {page}")
        return self._fetch_static_list_page(page)

    def _fetch_static_list_page(self, page):
        url = self._START_URL if page == 0 else f"{self._START_URL}?page={page}"
        return self._curl(
            url,
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            referer=self.base_url + "/",
        )

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_html(self, raw):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup {parser} failed: {exc}")
        return None

    @staticmethod
    def _clean_text(value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\xa0", " ")
        text = text.replace("\u200b", "")
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        text = re.sub(r"\s+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def _one_line(cls, value):
        return re.sub(r"\s+", " ", cls._clean_text(value)).strip()

    @staticmethod
    def _meta_content(soup, *keys):
        if soup is None:
            return ""
        for key in keys:
            node = soup.find("meta", attrs={"name": key})
            if node and node.get("content"):
                return node.get("content", "").strip()
            node = soup.find("meta", attrs={"property": key})
            if node and node.get("content"):
                return node.get("content", "").strip()
        return ""

    @staticmethod
    def _parse_date(raw):
        raw = UsgsGovNewsCrawler._one_line(raw)
        if not raw:
            return ""
        match = re.search(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b", raw)
        if match:
            return match.group(0)
        raw = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", raw)
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return ""

    @staticmethod
    def _split_keywords(raw):
        if not raw:
            return []
        return [
            item.strip()
            for item in re.split(r"\s*[,;]\s*", raw)
            if item and item.strip()
        ]

    def _extract_view_settings(self, raw):
        soup = self._parse_html(raw)
        if soup is None:
            return dict(self._DEFAULT_VIEW)

        script = soup.find("script", attrs={"data-drupal-selector": "drupal-settings-json"})
        if script and script.string:
            try:
                settings = json.loads(script.string)
                views = (settings.get("views") or {}).get("ajaxViews") or {}
                for config in views.values():
                    if (
                        isinstance(config, dict)
                        and config.get("view_name")
                        and config.get("view_display_id")
                    ):
                        merged = dict(self._DEFAULT_VIEW)
                        merged.update({k: v for k, v in config.items() if v is not None})
                        return merged
            except (json.JSONDecodeError, AttributeError, TypeError) as exc:
                print(f"[{self.site_id}] drupalSettings parse failed: {exc}")

        view = soup.select_one("div[class*='js-view-dom-id-']")
        if view:
            merged = dict(self._DEFAULT_VIEW)
            for cls in view.get("class", []):
                if cls.startswith("js-view-dom-id-"):
                    merged["view_dom_id"] = cls.replace("js-view-dom-id-", "", 1)
                    break
            return merged

        return dict(self._DEFAULT_VIEW)

    def _parse_list_items(self, raw):
        soup = self._parse_html(raw)
        if soup is None:
            return []

        items = []
        seen = set()
        cards = soup.select(".node--type--news.node--view-mode--teaser")
        for card in cards:
            link = card.select_one("a.d-link[href]") or card.select_one("a.green-link[href]")
            if link is None:
                continue
            url = urljoin(self.base_url, link.get("href", "").strip())
            if not url or url in seen:
                continue
            seen.add(url)

            title_node = card.select_one(".d-title")
            title = self._one_line(title_node.get_text(" ", strip=True) if title_node else "")
            if not title:
                title = self._one_line(link.get_text(" ", strip=True))

            time_node = card.select_one("time")
            date_raw = ""
            if time_node:
                date_raw = time_node.get("datetime") or time_node.get_text(" ", strip=True)

            teaser_node = card.select_one(".field-intro")
            teaser = self._one_line(
                teaser_node.get_text(" ", strip=True) if teaser_node else ""
            )
            category = self._category_from_url(url)

            items.append(
                {
                    "title": title,
                    "url": url,
                    "published_date": self._parse_date(date_raw),
                    "teaser": teaser,
                    "category": category,
                }
            )
        return items

    @staticmethod
    def _category_from_url(url):
        path = urlparse(url).path
        if "/national-news-release/" in path:
            return "National News Release"
        if "/state-news-release/" in path:
            return "State News Release"
        return "News Release"

    @staticmethod
    def _external_id_from_url(url):
        path = urlparse(url).path.strip("/")
        slug = path.rsplit("/", 1)[-1] if path else url
        return slug or url

    def _extract_text_from_container(self, container):
        if container is None:
            return ""
        for bad in container.select(
            "script, style, noscript, figure, .usa-sr-only, .d-media-copyright"
        ):
            bad.decompose()
        parts = []
        for node in container.find_all(["p", "li"], recursive=True):
            text = self._one_line(node.get_text(" ", strip=True))
            if text:
                parts.append(text)
        if not parts:
            text = self._one_line(container.get_text(" ", strip=True))
            if text:
                parts.append(text)
        return "\n\n".join(parts)

    def _parse_detail(self, raw, item):
        soup = self._parse_html(raw)
        if soup is None:
            raise ValueError("failed to parse detail HTML with all parsers")

        title = (
            self._meta_content(soup, "og:title")
            or self._one_line(
                soup.select_one(".carousel-title").get_text(" ", strip=True)
                if soup.select_one(".carousel-title")
                else ""
            )
            or item.get("title", "")
        )
        url = self._meta_content(soup, "og:url") or item.get("url", "")

        body = soup.find("body")
        node_id = ""
        if body:
            for cls in body.get("class", []):
                match = re.match(r"page-node-(\d+)$", cls)
                if match:
                    node_id = match.group(1)
                    break
        external_id = node_id or self._external_id_from_url(url)

        intro = self._extract_text_from_container(soup.select_one(".node-intro .field-intro"))
        body_text = self._extract_text_from_container(soup.select_one(".node-main-body"))
        abstract = "\n\n".join(part for part in (intro, body_text) if part).strip()
        if not abstract:
            abstract = self._meta_content(soup, "description", "og:description")
        abstract = self._clean_text(abstract)

        date_node = soup.select_one(".owner-date .date")
        published_date = self._parse_date(
            date_node.get_text(" ", strip=True) if date_node else ""
        )
        if not published_date:
            published_date = item.get("published_date") or ""

        byline_node = soup.select_one(".owner-date .by-line")
        byline = self._one_line(byline_node.get_text(" ", strip=True) if byline_node else "")
        byline = re.sub(r"^By\s+", "", byline).strip()
        authors = [byline] if byline else []

        category_node = soup.select_one(".carousel-above-title .usgs-breadcrumbs__link")
        category = self._one_line(
            category_node.get_text(" ", strip=True) if category_node else ""
        )
        if not category:
            category = item.get("category") or self._category_from_url(url)

        keywords = self._split_keywords(self._meta_content(soup, "keywords"))

        pdf_url = ""
        related_links = []
        for link in soup.select("a[href]"):
            href = urljoin(self.base_url, link.get("href", "").strip())
            label = self._one_line(link.get_text(" ", strip=True))
            if href and label:
                related_links.append({"label": label, "url": href})
            if re.search(r"\.pdf(?:[?#]|$)", href, flags=re.I):
                pdf_url = href
                break

        doi = ""
        match = re.search(r"https?://(?:dx\.)?doi\.org/(10\.[^\s\"'<>]+)", raw or "")
        if match:
            doi = match.group(1).rstrip(".,)")

        metadata = {
            "source": "USGS Drupal Views AJAX + detail HTML",
            "list_api_url": self._VIEWS_AJAX_URL,
            "detail_endpoint": "public detail HTML page",
            "canonical_url": url,
            "teaser": item.get("teaser") or "",
            "node_id": node_id,
            "related_links": related_links[:20],
        }

        return {
            "id": external_id,
            "site_id": self.site_id,
            "external_id": external_id,
            "title": self._one_line(title),
            "authors": json.dumps(authors, ensure_ascii=False),
            "abstract": abstract,
            "category": category,
            "keywords": json.dumps(keywords, ensure_ascii=False),
            "published_date": published_date,
            "url": url,
            "pdf_url": pdf_url,
            "doi": doi,
            "department": byline or "U.S. Geological Survey",
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 0

        start_html = self._fetch_start_html()
        if not start_html:
            print(f"[{self.site_id}] failed to fetch start page")
            return 0
        view_settings = self._extract_view_settings(start_html)

        while True:
            if limit is not None and saved >= limit:
                break

            list_html = self._fetch_list_page_html(page, view_settings)
            if not list_html:
                print(f"[{self.site_id}] empty list page {page}; stopping")
                break

            items = self._parse_list_items(list_html)
            if not items:
                print(f"[{self.site_id}] no list items on page {page}; stopping")
                break

            for idx, item in enumerate(items, start=1):
                if limit is not None and saved >= limit:
                    break

                item_label = item.get("url") or f"page {page} item {idx}"
                try:
                    time.sleep(self.detail_delay)
                    detail_html = self._fetch_detail_html(item["url"])
                    if not detail_html:
                        print(f"[{self.site_id}] item {item_label} failed: empty detail")
                        continue

                    paper = self._parse_detail(detail_html, item)
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            page += 1

        return saved
