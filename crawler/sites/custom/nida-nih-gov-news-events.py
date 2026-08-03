# -*- coding: utf-8 -*-
"""Crawler for NIDA news releases."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class NidaNihGovNewsEventsCrawler(BaseCrawler):
    site_id = "nida-nih-gov-news-events"
    site_name = "Custom: nida-nih-gov-news-events"
    base_url = "https://nida.nih.gov"

    _START_URL = "https://nida.nih.gov/news-events/news-releases"
    _LIST_ENDPOINT = "https://nida.nih.gov/news-events/news-releases?page={page}"
    _DETAIL_ENDPOINT = "Drupal article HTML linked from news release list pages"
    _CATEGORY = "News Release"
    _DEPARTMENT = "National Institute on Drug Abuse"
    _RETRY_WAITS = (1, 3, 9)
    _SAFETY_PAGE_CAP = 200
    _MAX_CRAWL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _TIME_STOP_MARGIN_SECONDS = 30
    _MIN_ABSTRACT_CHARS = 50

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, *, context="request", accept=None, referer=None, timeout=45):
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
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                if result.returncode == 0 and raw.strip():
                    return raw
                if result.returncode == 0:
                    last_error = "empty response"
                else:
                    last_error = f"exit={result.returncode} stderr={stderr[:500]}"

            if attempt < 3:
                wait = self._RETRY_WAITS[attempt - 1]
                print(
                    f"[{self.site_id}] {context} curl failed "
                    f"{attempt}/3 for {url}: {last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None

    def _fetch_list_page(self, page):
        url = self._START_URL if page == 0 else self._LIST_ENDPOINT.format(page=page)
        return self._curl_get(
            url,
            context=f"list page {page}",
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            referer=self.base_url + "/",
        )

    def _fetch_detail_page(self, url):
        return self._curl_get(
            url,
            context=f"detail {url}",
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            referer=self._START_URL,
        )

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_html(self, raw, *, context="html"):
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", errors="replace")
        else:
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
    def _external_id_from_url(url):
        path = urlparse(url).path.strip("/")
        if not path:
            return url
        return path.rsplit("/", 1)[-1] or path

    @classmethod
    def _parse_date(cls, raw):
        raw = cls._one_line(raw)
        if not raw or raw == "--":
            return ""

        match = re.search(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b", raw)
        if match:
            return match.group(0)

        raw = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", raw, flags=re.I)
        for fmt in (
            "%B %d, %Y",
            "%b %d, %Y",
            "%m/%d/%Y",
            "%m/%d/%y",
            "%d %B %Y",
            "%d %b %Y",
        ):
            try:
                return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return ""

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

    @classmethod
    def _split_keywords(cls, raw):
        if not raw:
            return []
        return [
            cls._one_line(item)
            for item in re.split(r"\s*[,;]\s*", raw)
            if cls._one_line(item)
        ]

    def _extract_json_ld(self, soup):
        if soup is None:
            return {}
        for script in soup.select("script[type='application/ld+json']"):
            raw = script.string or script.get_text("", strip=True)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                continue
            candidates = []
            if isinstance(data, dict):
                candidates.append(data)
                graph = data.get("@graph")
                if isinstance(graph, list):
                    candidates.extend(x for x in graph if isinstance(x, dict))
            for candidate in candidates:
                type_value = candidate.get("@type")
                if type_value == "NewsArticle" or (
                    isinstance(type_value, list) and "NewsArticle" in type_value
                ):
                    return candidate
        return {}

    def _parse_list_items(self, soup):
        if soup is None:
            return []

        view = (
            soup.select_one(".views-element-container .media-list")
            or soup.select_one(".media-list")
            or soup.select_one("main")
            or soup
        )
        items = []
        seen = set()
        for article in view.select("article[data-history-node-id]"):
            link = article.select_one("h3.media-item-title a[href]")
            if link is None:
                continue

            url = urljoin(self.base_url, link.get("href", "").strip())
            parsed = urlparse(url)
            if parsed.netloc and parsed.netloc != urlparse(self.base_url).netloc:
                continue
            if not parsed.path.startswith("/news-events/news-releases/"):
                continue
            if url in seen:
                continue
            seen.add(url)

            title = self._one_line(link.get_text(" ", strip=True))
            if not title:
                continue

            time_node = article.select_one("time")
            raw_date = ""
            if time_node:
                raw_date = time_node.get("datetime") or time_node.get_text(" ", strip=True)

            blurb_node = article.select_one(".media-item-blurb")
            list_summary = ""
            if blurb_node:
                blurb_copy = BeautifulSoup(str(blurb_node), "html.parser")
                for node in blurb_copy.select("time, .meta-data"):
                    node.decompose()
                list_summary = self._one_line(blurb_copy.get_text(" ", strip=True))
                list_summary = re.sub(r"^\|\s*", "", list_summary).strip()

            items.append(
                {
                    "title": title,
                    "url": url,
                    "published_date": self._parse_date(raw_date),
                    "node_id": article.get("data-history-node-id", "") or "",
                    "list_summary": list_summary,
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
                ".pagination-next a[href], "
                "a[aria-label='Next page'][href]"
            )
        )

    def _collect_links(self, container):
        links = []
        if container is None:
            return links
        seen = set()
        for link in container.select("a[href]"):
            href = urljoin(self.base_url, link.get("href", "").strip())
            label = self._one_line(link.get_text(" ", strip=True))
            if not href or href in seen:
                continue
            seen.add(href)
            links.append({"label": label, "url": href})
        return links

    def _find_pdf_url(self, container):
        if container is None:
            return ""
        for link in container.select("a[href]"):
            href = urljoin(self.base_url, link.get("href", "").strip())
            if re.search(r"\.pdf(?:[?#]|$)", href, flags=re.I):
                return href
        return ""

    def _extract_article_text(self, article):
        if article is None:
            return ""

        stop_prefixes = (
            "about the national institute on drug abuse",
            "about the national institutes of health",
            "about substance use disorders",
            "for more information on substance and mental health treatment programs",
            "nih...turning discovery into health",
            "nih\u2026turning discovery into health",
        )
        skip_exact = {
            "###",
            "high-res image",
            "high-res image opens in new window",
        }
        parts = []
        for node in article.find_all(["h2", "h3", "p", "li"], recursive=True):
            text = self._one_line(node.get_text(" ", strip=True))
            if not text:
                continue

            lower = text.lower()
            if lower in skip_exact:
                break
            if node.name in {"h2", "h3"} and lower.startswith(("reference", "references")):
                break
            if lower.startswith(stop_prefixes):
                break
            if lower.startswith("related articles"):
                break

            if node.name in {"h2", "h3"}:
                continue
            parts.append(text)

        if not parts:
            text = self._one_line(article.get_text(" ", strip=True))
            if text:
                parts.append(text)
        return self._clean_text("\n\n".join(parts))

    def _extract_doi(self, raw):
        if not raw:
            return ""
        patterns = (
            r"https?://(?:dx\.)?doi\.org/(10\.[^\s\"'<>]+)",
            r"\bDOI:\s*(10\.[^\s\"'<>]+)",
        )
        for pattern in patterns:
            match = re.search(pattern, raw, flags=re.I)
            if match:
                return match.group(1).rstrip(".,);")
        return ""

    def _parse_detail(self, raw, item):
        soup = self._parse_html(raw, context=item.get("url") or "detail")
        if soup is None:
            raise ValueError("failed to parse detail HTML with all parsers")

        article = (
            soup.select_one("article[data-history-node-id].content-river")
            or soup.select_one("article[data-history-node-id]")
            or soup.select_one("main article")
            or soup.select_one("main")
        )
        if article is None:
            raise ValueError("detail page has no article/main content")

        json_ld = self._extract_json_ld(soup)
        canonical_node = soup.find("link", rel=lambda value: value and "canonical" in value)
        canonical_url = (
            canonical_node.get("href", "").strip()
            if canonical_node and canonical_node.get("href")
            else item.get("url", "")
        )
        canonical_url = urljoin(self.base_url, canonical_url)

        node_id = article.get("data-history-node-id", "") or item.get("node_id", "") or ""
        external_id = node_id or self._external_id_from_url(canonical_url)

        title_node = (
            soup.select_one(".page-banner--title h1")
            or article.select_one("h1")
            or soup.select_one("h1")
        )
        title = self._one_line(title_node.get_text(" ", strip=True) if title_node else "")
        if not title:
            title = self._one_line(json_ld.get("headline") or item.get("title") or "")
        title = re.sub(r"\s*\|\s*National Institute on Drug Abuse.*$", "", title).strip()

        published_date = (
            self._parse_date(json_ld.get("datePublished") or "")
            or self._parse_date(self._meta_content(soup, "article:published_time"))
            or self._parse_date(
                article.select_one("time").get("datetime")
                if article.select_one("time")
                else ""
            )
            or item.get("published_date", "")
        )
        modified_date = (
            self._parse_date(json_ld.get("dateModified") or "")
            or self._parse_date(self._meta_content(soup, "article:modified_time", "og:updated_time"))
        )

        abstract = self._extract_article_text(article)
        if len(abstract) < self._MIN_ABSTRACT_CHARS:
            abstract = self._one_line(
                self._meta_content(soup, "description", "abstract", "og:description")
                or json_ld.get("description")
                or item.get("list_summary", "")
            )

        keywords = self._split_keywords(self._meta_content(soup, "keywords"))
        for tag in soup.select("meta[property='article:tag']"):
            tag_value = self._one_line(tag.get("content", ""))
            if tag_value and tag_value not in keywords:
                keywords.append(tag_value)
        if self._CATEGORY not in keywords:
            keywords.insert(0, self._CATEGORY)

        author_name = self._meta_content(soup, "article:author")
        authors = [author_name] if author_name else [self._DEPARTMENT]
        image = self._meta_content(soup, "og:image:url", "twitter:image")
        pdf_url = self._find_pdf_url(article)
        links = self._collect_links(article)

        metadata = {
            "source": "NIDA Drupal rendered HTML",
            "list_endpoint": self._LIST_ENDPOINT,
            "detail_endpoint": self._DETAIL_ENDPOINT,
            "canonical_url": canonical_url,
            "node_id": node_id,
            "list_title": item.get("title", ""),
            "list_date": item.get("published_date", ""),
            "list_summary": item.get("list_summary", ""),
            "modified_date": modified_date,
            "image": image,
            "links": links[:50],
        }

        return {
            "id": external_id,
            "site_id": self.site_id,
            "external_id": external_id,
            "title": title,
            "authors": json.dumps(authors, ensure_ascii=False),
            "abstract": abstract,
            "category": self._CATEGORY,
            "keywords": json.dumps(keywords, ensure_ascii=False),
            "published_date": published_date,
            "url": canonical_url,
            "pdf_url": pdf_url,
            "doi": self._extract_doi(raw),
            "department": self._DEPARTMENT,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _time_budget_exhausted(self, started_at):
        elapsed = time.monotonic() - started_at
        return elapsed >= self._MAX_CRAWL_SECONDS - self._TIME_STOP_MARGIN_SECONDS

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 0
        seen_urls = set()
        started_at = time.monotonic()
        limit_or_inf = limit if limit is not None else "inf"
        hit_page_cap = False

        try:
            while page < self._SAFETY_PAGE_CAP:
                if limit is not None and saved >= limit:
                    break
                if self._time_budget_exhausted(started_at):
                    print(f"[{self.site_id}] approaching 25 minute crawl budget; stopping cleanly")
                    break
                if page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

                list_html = self._fetch_list_page(page)
                if not list_html:
                    print(f"[{self.site_id}] empty list page {page}; stopping")
                    break

                soup = self._parse_html(list_html, context=f"list page {page}")
                if soup is None:
                    print(f"[{self.site_id}] list page {page} parse failed; stopping")
                    break

                items = self._parse_list_items(soup)
                if not items:
                    print(f"[{self.site_id}] no list items on page {page}; stopping")
                    break

                new_records_on_page = 0
                for idx, item in enumerate(items, start=1):
                    if limit is not None and saved >= limit:
                        break
                    if self._time_budget_exhausted(started_at):
                        print(f"[{self.site_id}] approaching 25 minute crawl budget; stopping cleanly")
                        return saved

                    item_url = item.get("url") or f"page {page} item {idx}"
                    if item_url in seen_urls:
                        continue
                    seen_urls.add(item_url)
                    new_records_on_page += 1

                    try:
                        time.sleep(self.detail_delay)
                        detail_html = self._fetch_detail_page(item_url)
                        if not detail_html:
                            print(f"[{self.site_id}] item {item_url} failed: empty detail response")
                            continue

                        paper = self._parse_detail(detail_html, item)
                        abstract = paper.get("abstract") or ""
                        if len(abstract) < self._MIN_ABSTRACT_CHARS:
                            print(
                                f"[{self.site_id}] item {item_url} skipped: "
                                f"abstract too short ({len(abstract)} chars)"
                            )
                            continue

                        self._save_paper(paper)
                        saved += 1
                        print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:80]}")
                    except Exception as exc:
                        print(f"[{self.site_id}] item {item_url} failed: {exc}")
                        continue

                if limit is not None and saved >= limit:
                    break
                if new_records_on_page == 0:
                    print(f"[{self.site_id}] page {page} had 0 new records; stopping")
                    break
                if not self._has_next_page(soup):
                    print(f"[{self.site_id}] no next page after page {page}; stopping")
                    break

                page += 1
            else:
                hit_page_cap = True
        except KeyboardInterrupt:
            print(f"[{self.site_id}] interrupted by user")
            raise

        if hit_page_cap:
            print(f"[{self.site_id}] reached safety page cap of {self._SAFETY_PAGE_CAP}; stopping")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
