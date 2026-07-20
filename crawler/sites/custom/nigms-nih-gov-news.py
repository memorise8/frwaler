# -*- coding: utf-8 -*-
"""Crawler for NIGMS releases and announcements."""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class NigmsNihGovNewsCrawler(BaseCrawler):
    site_id = "nigms-nih-gov-news"
    site_name = "Custom: nigms-nih-gov-news"
    base_url = "https://www.nigms.nih.gov"

    _START_URL = "https://www.nigms.nih.gov/news/releases-and-announcements"
    _LIST_ENDPOINT = _START_URL
    _DETAIL_ENDPOINT = "public Drupal detail HTML pages linked from the list view"
    _CATEGORY = "Releases and Announcements"
    _MIN_ABSTRACT_CHARS = 100
    _RETRY_WAITS = (1, 3, 9)

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
            "--fail",
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
        cmd.append(url)

        last_error = "unknown error"
        for attempt in range(1, 4):
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
                if result.returncode == 0:
                    last_error = "empty response"
                else:
                    last_error = f"exit={result.returncode} stderr={stderr[:500]}"

            if attempt < 3:
                wait = self._RETRY_WAITS[attempt - 1]
                print(
                    f"[{self.site_id}] curl failed {attempt}/3 for {url}: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    def _fetch_list_page(self, page):
        url = self._START_URL if page == 0 else f"{self._START_URL}?page={page}"
        return self._curl(
            url,
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            referer=self.base_url + "/",
        )

    def _fetch_detail_html(self, url):
        return self._curl(
            url,
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            referer=self._START_URL,
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
        text = text.replace("\xa0", " ").replace("\u200b", "")
        text = text.replace("\r", "\n").replace("\f", "\n")
        text = re.sub(r"[ \t\v]+", " ", text)
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
        raw = NigmsNihGovNewsCrawler._one_line(raw)
        if not raw:
            return ""

        match = re.search(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b", raw)
        if match:
            return match.group(0)

        raw = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", raw, flags=re.I)
        for fmt in ("%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y"):
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

    @staticmethod
    def _external_id_from_url(url):
        path = urlparse(url).path.strip("/")
        if not path:
            return url
        return path.rsplit("/", 1)[-1] or path

    def _parse_list_items_from_soup(self, soup):
        if soup is None:
            return []

        view = (
            soup.select_one(".view-news.view-display-id-page_1")
            or soup.select_one(".view-news")
            or soup.select_one("main")
            or soup
        )
        items = []
        seen = set()
        for row in view.select(".views-row"):
            link = row.select_one("a[href]")
            if link is None:
                continue

            url = urljoin(self.base_url, link.get("href", "").strip())
            if not url or url in seen:
                continue
            seen.add(url)

            title = self._one_line(link.get_text(" ", strip=True))
            if not title:
                continue

            time_node = row.select_one("time")
            raw_date = ""
            if time_node:
                raw_date = time_node.get("datetime") or time_node.get_text(" ", strip=True)

            department = "National Institute of General Medical Sciences"
            row_text = self._one_line(row.get_text(" ", strip=True))
            if "\u2022" in row_text:
                candidate = self._one_line(row_text.split("\u2022", 1)[1])
                if candidate:
                    department = candidate

            items.append(
                {
                    "title": title,
                    "url": url,
                    "published_date": self._parse_date(raw_date),
                    "department": department,
                    "list_text": row_text,
                }
            )
        return items

    @staticmethod
    def _has_next_page(soup):
        if soup is None:
            return False
        return bool(
            soup.select_one(
                "nav.pager a[rel='next'], "
                ".pager__item--next a[href], "
                ".pager .next a[href], "
                "a[aria-label='Next page'][href]"
            )
        )

    def _extract_body_text(self, container):
        if container is None:
            return ""

        for bad in container.select(
            "script, style, noscript, figure, img, .news-headshot-container, "
            ".last-updated"
        ):
            bad.decompose()

        parts = []
        for node in container.find_all(["p", "li"], recursive=True):
            text = self._one_line(node.get_text(" ", strip=True))
            if not text:
                continue
            if text.lower() in {"high-res image", "high-res image opens in new window"}:
                continue
            parts.append(text)

        if not parts:
            text = self._one_line(container.get_text(" ", strip=True))
            if text:
                parts.append(text)

        return self._clean_text("\n\n".join(parts))

    def _article_lines(self, article):
        text = self._clean_text(article.get_text("\n", strip=True) if article else "")
        return [self._one_line(line) for line in text.splitlines() if self._one_line(line)]

    def _extract_detail_date(self, article):
        for idx, line in enumerate(self._article_lines(article)):
            if re.search(r"\b(announcement|release|published)\s+date\b", line, re.I):
                for candidate in self._article_lines(article)[idx + 1:idx + 5]:
                    parsed = self._parse_date(candidate)
                    if parsed:
                        return parsed
        return ""

    def _extract_last_updated(self, article):
        lines = self._article_lines(article)
        for idx, line in enumerate(lines):
            if "last updated" in line.lower():
                for candidate in lines[idx:idx + 4]:
                    parsed = self._parse_date(candidate)
                    if parsed:
                        return parsed
        return ""

    def _extract_contact(self, article):
        if article is None:
            return ""
        node = article.select_one(".field--name-field-contact-information .field__item")
        if node is None:
            node = article.select_one(".field--name-field-contact-information")
        return self._one_line(node.get_text(" ", strip=True) if node else "")

    def _find_pdf_url(self, soup):
        if soup is None:
            return ""
        for link in soup.select("a[href]"):
            href = urljoin(self.base_url, link.get("href", "").strip())
            if re.search(r"\.pdf(?:[?#]|$)", href, flags=re.I):
                return href
        return ""

    def _collect_links(self, article):
        links = []
        if article is None:
            return links
        seen = set()
        for link in article.select("a[href]"):
            href = urljoin(self.base_url, link.get("href", "").strip())
            label = self._one_line(link.get_text(" ", strip=True))
            if not href or href in seen:
                continue
            seen.add(href)
            links.append({"label": label, "url": href})
        return links

    def _fallback_article_text(self, article):
        if article is None:
            return ""
        for bad in article.select(
            "script, style, noscript, h1, .field--name-title, "
            ".field--name-field-contact-information, .last-updated"
        ):
            bad.decompose()
        return self._extract_body_text(article)

    def _parse_detail(self, raw, item):
        soup = self._parse_html(raw)
        if soup is None:
            raise ValueError("failed to parse detail HTML with all parsers")

        article = (
            soup.select_one("article[data-history-node-id]")
            or soup.select_one("article")
            or soup.select_one("#block-nigms-main-content")
            or soup.select_one("main")
        )
        if article is None:
            raise ValueError("detail page has no article/main content")

        canonical_node = soup.find("link", rel=lambda value: value and "canonical" in value)
        canonical_url = (
            canonical_node.get("href", "").strip()
            if canonical_node and canonical_node.get("href")
            else item.get("url", "")
        )
        canonical_url = urljoin(self.base_url, canonical_url)

        node_id = article.get("data-history-node-id", "") or ""
        external_id = node_id or self._external_id_from_url(canonical_url)

        title_node = article.select_one(".field--name-title") or article.select_one("h1")
        title = self._one_line(title_node.get_text(" ", strip=True) if title_node else "")
        if not title:
            title = item.get("title") or self._one_line(soup.title.string if soup.title else "")
        title = re.sub(r"\s*\|\s*National Institute of General Medical Sciences\s*$", "", title)

        body_node = (
            article.select_one(".field--name-body.field--type-text-with-summary")
            or article.select_one(".field--name-body")
        )
        abstract = self._extract_body_text(body_node)
        if len(abstract) < 50:
            abstract = self._fallback_article_text(article)

        published_date = self._extract_detail_date(article) or item.get("published_date") or ""
        contact = self._extract_contact(article)
        last_updated = self._extract_last_updated(article)

        keywords = self._split_keywords(self._meta_content(soup, "keywords"))
        pdf_url = self._find_pdf_url(article)

        doi = ""
        match = re.search(r"https?://(?:dx\.)?doi\.org/(10\.[^\s\"'<>]+)", raw or "")
        if match:
            doi = match.group(1).rstrip(".,)")

        metadata = {
            "source": "NIGMS Drupal rendered HTML",
            "list_endpoint": self._LIST_ENDPOINT,
            "detail_endpoint": self._DETAIL_ENDPOINT,
            "canonical_url": canonical_url,
            "node_id": node_id,
            "list_title": item.get("title") or "",
            "list_date": item.get("published_date") or "",
            "list_text": item.get("list_text") or "",
            "contact": contact,
            "last_updated": last_updated,
            "links": self._collect_links(article)[:30],
        }

        return {
            "id": external_id,
            "site_id": self.site_id,
            "external_id": external_id,
            "title": title,
            "authors": json.dumps([], ensure_ascii=False),
            "abstract": abstract,
            "category": self._CATEGORY,
            "keywords": json.dumps(keywords, ensure_ascii=False),
            "published_date": published_date,
            "url": canonical_url,
            "pdf_url": pdf_url,
            "doi": doi,
            "department": contact or item.get("department") or "National Institute of General Medical Sciences",
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 0
        seen_urls = set()

        while True:
            if limit is not None and saved >= limit:
                break

            list_html = self._fetch_list_page(page)
            if not list_html:
                print(f"[{self.site_id}] empty list page {page}; stopping")
                break

            soup = self._parse_html(list_html)
            if soup is None:
                print(f"[{self.site_id}] list page {page} parse failed; stopping")
                break

            items = self._parse_list_items_from_soup(soup)
            if not items:
                print(f"[{self.site_id}] no list items on page {page}; stopping")
                break

            for idx, item in enumerate(items, start=1):
                if limit is not None and saved >= limit:
                    break

                item_label = item.get("url") or f"page {page} item {idx}"
                if item_label in seen_urls:
                    continue
                seen_urls.add(item_label)

                try:
                    time.sleep(self.detail_delay)
                    detail_html = self._fetch_detail_html(item["url"])
                    if not detail_html:
                        print(f"[{self.site_id}] item {item_label} failed: empty detail")
                        continue

                    paper = self._parse_detail(detail_html, item)
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self._MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit else str(saved)
                    print(f"[{self.site_id}] Saved {counter}: {paper.get('title', '')[:80]}")
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break
            if not self._has_next_page(soup):
                break
            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
