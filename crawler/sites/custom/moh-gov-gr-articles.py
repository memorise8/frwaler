# -*- coding: utf-8 -*-
"""Crawler for moh.gov.gr Ministry press releases."""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class MohGovGrArticlesCrawler(BaseCrawler):
    site_id = "moh-gov-gr-articles"
    site_name = "Custom: moh-gov-gr-articles"
    base_url = "https://www.moh.gov.gr"

    START_URL = "https://www.moh.gov.gr/articles/ministry/grafeio-typoy/press-releases"
    BACKOFF_SECONDS = (1, 3, 9)
    CURL_TIMEOUT = 45
    MIN_ABSTRACT_CHARS = 50
    MIN_SAVED_ABSTRACT_CHARS = 100
    _CURL_META_MARKER = "__MOH_GOV_GR_CURL_META__:"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl the press-release HTML list and public detail pages."""
        saved = 0
        page = 1
        seen_urls = set()

        while True:
            if limit is not None and saved >= limit:
                break

            list_url = self._list_url(page)
            raw = self._curl_get(
                list_url,
                context=f"list page {page}",
                referer=self.base_url + "/",
            )
            if not raw:
                print(f"[{self.site_id}] list endpoint failed at page {page}; stopping")
                break

            soup = self._make_soup(raw, context=f"list page {page}")
            if soup is None:
                print(f"[{self.site_id}] list page {page} could not be parsed; stopping")
                break

            records = self._parse_list(soup, list_url)
            if not records:
                print(f"[{self.site_id}] no records found at page {page}; stopping")
                break

            print(
                f"[{self.site_id}] page {page}: discovered {len(records)} "
                "records from HTML list endpoint"
            )

            for idx, record in enumerate(records, start=1):
                if limit is not None and saved >= limit:
                    break

                item_label = f"{page}.{idx}"
                try:
                    detail_url = record.get("url") or ""
                    if not detail_url:
                        raise RuntimeError("record has no detail URL")
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)

                    time.sleep(self.detail_delay)
                    detail_raw = self._curl_get(
                        detail_url,
                        context=f"item {item_label} detail",
                        referer=list_url,
                    )
                    if not detail_raw:
                        raise RuntimeError("detail fetch failed after retries")

                    detail_soup = self._make_soup(
                        detail_raw,
                        context=f"item {item_label} detail",
                    )
                    if detail_soup is None:
                        raise RuntimeError("detail HTML could not be parsed")

                    parsed = self._parse_detail(detail_soup, detail_raw, record)
                    abstract = parsed.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract shorter than 50 chars ({len(abstract)} chars)"
                        )
                        continue
                    if len(abstract) < self.MIN_SAVED_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract below save threshold ({len(abstract)} chars)"
                        )
                        continue

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": parsed["external_id"],
                        "title": parsed["title"],
                        "authors": json.dumps(parsed["authors"], ensure_ascii=False),
                        "abstract": abstract,
                        "category": parsed["category"],
                        "keywords": json.dumps(parsed["keywords"], ensure_ascii=False),
                        "published_date": parsed["published_date"],
                        "url": parsed["url"],
                        "pdf_url": parsed["pdf_url"],
                        "doi": parsed["doi"],
                        "department": parsed["department"],
                        "metadata": json.dumps(parsed["metadata"], ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {parsed['title'][:90]}")
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break
            if not self._has_next_page(soup, page):
                break
            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, context="request", referer=None, accept=None):
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
            (
                "Accept: "
                + (
                    accept
                    or "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "application/json,*/*;q=0.8"
                )
            ),
            "-H",
            "Accept-Language: el-GR,el;q=0.9,en;q=0.7",
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
                stdout = (result.stdout or b"").decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                body, http_code, effective_url = self._split_curl_output(stdout, url)

                if result.returncode != 0:
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if http_code and int(http_code) >= 400:
                    raise RuntimeError(f"HTTP {http_code} for {effective_url}")
                if not body.strip():
                    raise RuntimeError(f"empty response for {effective_url}")
                return body
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
        return None

    def _split_curl_output(self, raw, fallback_url):
        marker_pos = raw.rfind("\n" + self._CURL_META_MARKER)
        if marker_pos == -1:
            return raw, "", fallback_url
        body = raw[:marker_pos]
        meta = raw[marker_pos + 1 + len(self._CURL_META_MARKER):].strip()
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

    def _parse_list(self, soup, list_url):
        records = []
        seen = set()
        container = soup.select_one("#page_content.list") or soup.select_one("#page_content") or soup

        for card in container.select("div.item > article"):
            link = card.select_one("h1 a[href]")
            if link is None:
                continue

            url = urljoin(self.base_url, link.get("href", "").strip())
            if not self._is_press_detail_url(url) or url in seen:
                continue
            seen.add(url)

            title = self._node_text(link)
            if not title:
                continue

            date_raw = self._node_text(card.select_one(".datetime"))
            category = self._node_text(card.select_one(".category a")) or "Press releases"
            teaser = self._node_text(card.select_one(".description"))

            records.append(
                {
                    "title": title,
                    "url": url,
                    "external_id": self._external_id_from_url(url),
                    "published_date": self._parse_date(date_raw),
                    "category": category,
                    "teaser": teaser,
                    "list_date_text": date_raw,
                    "list_endpoint": list_url,
                }
            )

        return records

    def _parse_detail(self, soup, raw_html, record):
        article_data = self._find_news_article_jsonld(soup)
        canonical_url = (
            self._link_href(soup, "link[rel='canonical']")
            or self._meta_content(soup, "og:url")
            or article_data.get("url")
            or record.get("url")
            or ""
        )
        url = urljoin(self.base_url, canonical_url)

        article_node = (
            soup.select_one("article[id^='article_section_']")
            or soup.select_one("#page_content article")
            or soup.select_one("main article")
        )

        title = (
            self._clean_title(article_data.get("headline"))
            or self._clean_title(article_data.get("alternativeHeadline"))
            or self._node_text(article_node.select_one("h1") if article_node else None)
            or self._clean_title(self._meta_content(soup, "og:title", "twitter:title"))
            or record.get("title", "")
        )
        title = self._clean_title(title)
        if not title:
            raise RuntimeError("detail page has no title")

        abstract = (
            self._clean_multiline(article_data.get("articleBody"))
            or self._extract_article_text(article_node)
            or self._clean_multiline(article_data.get("description"))
            or self._clean_multiline(self._meta_content(soup, "description", "og:description"))
            or record.get("teaser", "")
        )

        published_date = (
            self._parse_date(article_data.get("datePublished"))
            or self._parse_date(article_data.get("dateCreated"))
            or self._parse_date(self._node_text(article_node.select_one(".datetime") if article_node else None))
            or record.get("published_date")
            or ""
        )
        modified_date = self._parse_date(article_data.get("dateModified"))

        category = (
            self._one_line(article_data.get("articleSection"))
            or record.get("category")
            or "Press releases"
        )

        keywords = self._parse_keywords(article_data.get("keywords"))
        if category and category not in keywords:
            keywords.append(category)

        authors = self._parse_people(article_data.get("author"))
        publisher = self._parse_org_name(article_data.get("publisher")) or "Ministry of Health, Greece"

        pdf_links = []
        attachment_links = []
        related_links = []
        if article_node is not None:
            for link in article_node.select("a[href]"):
                href = urljoin(self.base_url, link.get("href", "").strip())
                label = self._node_text(link)
                if not href or href.startswith("mailto:"):
                    continue
                if self._is_share_url(href):
                    continue
                if self._looks_like_pdf(href):
                    pdf_links.append({"label": label, "url": href})
                elif self._looks_like_attachment(href):
                    attachment_links.append({"label": label, "url": href})
                elif self._is_press_detail_url(href) and href != url:
                    related_links.append({"label": label, "url": href})
        pdf_links = self._dedupe_dicts(pdf_links, "url")
        attachment_links = self._dedupe_dicts(attachment_links, "url")
        related_links = self._dedupe_dicts(related_links, "url")
        pdf_url = pdf_links[0]["url"] if pdf_links else ""

        image_url = (
            self._image_url(article_data.get("image"))
            or self._meta_content(soup, "og:image", "twitter:image")
        )
        article_id = self._article_id_from_html(raw_html) or self._external_id_from_url(url)

        metadata = {
            "source": "moh.gov.gr server-rendered HTML list and NewsArticle detail HTML",
            "list_endpoint": record.get("list_endpoint", self.START_URL),
            "detail_endpoint": "public detail HTML page",
            "list_date_text": record.get("list_date_text", ""),
            "canonical_url": url,
            "content_id": article_id,
            "modified_date": modified_date,
            "json_ld_present": bool(article_data),
            "image_url": image_url,
            "teaser": record.get("teaser", ""),
            "attachments": attachment_links,
            "pdf_links": pdf_links,
            "related_links": related_links[:20],
        }

        return {
            "external_id": article_id,
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "category": category,
            "keywords": keywords,
            "published_date": published_date,
            "url": url,
            "pdf_url": pdf_url,
            "doi": "",
            "department": publisher,
            "metadata": metadata,
        }

    def _find_news_article_jsonld(self, soup):
        for obj in self._iter_jsonld(soup):
            if not isinstance(obj, dict):
                continue
            types = obj.get("@type")
            if isinstance(types, str):
                types = [types]
            if not isinstance(types, list):
                types = []
            if any(t in {"NewsArticle", "Article"} for t in types):
                return obj
        return {}

    def _iter_jsonld(self, soup):
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string
            if raw is None:
                raw = script.get_text("", strip=False)
            text = str(raw or "").strip()
            if not text:
                continue

            data = None
            try:
                data = json.loads(text)
            except (TypeError, ValueError):
                cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
                try:
                    data = json.loads(cleaned)
                except (TypeError, ValueError) as exc:
                    print(f"[{self.site_id}] JSON-LD parse failed: {exc}")
                    continue

            if isinstance(data, list):
                for item in data:
                    yield item
            elif isinstance(data, dict):
                graph = data.get("@graph")
                if isinstance(graph, list):
                    for item in graph:
                        yield item
                yield data

    def _extract_article_text(self, article_node):
        if article_node is None:
            return ""

        fragment = self._make_soup(str(article_node), context="detail article fragment")
        if fragment is None:
            return ""

        for bad in fragment.select(
            "script, style, noscript, iframe, svg, figure, .meta, .share, "
            ".extra_articles, .related, h1, .datetime"
        ):
            bad.decompose()

        parts = []
        for node in fragment.find_all(["p", "li", "h2", "h3"], recursive=True):
            text = self._node_text(node)
            if text and text not in parts:
                parts.append(text)

        if parts:
            return self._clean_multiline("\n\n".join(parts))

        return self._clean_multiline(fragment.get_text("\n", strip=True))

    # ------------------------------------------------------------------
    # Small utilities
    # ------------------------------------------------------------------

    def _list_url(self, page):
        if page <= 1:
            return self.START_URL
        return f"{self.START_URL}?page={page}"

    def _has_next_page(self, soup, page):
        next_link = soup.select_one("a.paging_next[href]")
        if next_link is not None and next_link.get("href"):
            return True
        return bool(soup.select_one(f"a[href*='page={page + 1}']"))

    @classmethod
    def _parse_date(cls, value):
        text = cls._one_line(value)
        if not text:
            return ""

        iso = re.search(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b", text)
        if iso:
            return iso.group(0)

        slash = re.search(r"\b(\d{1,2})/(\d{1,2})/((?:19|20)\d{2})\b", text)
        if slash:
            day = int(slash.group(1))
            month = int(slash.group(2))
            year = int(slash.group(3))
            if 1 <= day <= 31 and 1 <= month <= 12:
                return f"{year:04d}-{month:02d}-{day:02d}"

        return ""

    @staticmethod
    def _meta_content(soup, *keys):
        for key in keys:
            node = soup.find("meta", attrs={"name": key})
            if node and node.get("content"):
                return node.get("content", "").strip()
            node = soup.find("meta", attrs={"property": key})
            if node and node.get("content"):
                return node.get("content", "").strip()
        return ""

    @staticmethod
    def _link_href(soup, selector):
        node = soup.select_one(selector)
        if node and node.get("href"):
            return node.get("href", "").strip()
        return ""

    @classmethod
    def _clean_title(cls, value):
        title = cls._one_line(value)
        title = re.sub(r"\s*-\s*Ypoyrgeio.*$", "", title, flags=re.I)
        return title.strip()

    @staticmethod
    def _clean_text(value):
        text = unescape(str(value or ""))
        text = text.replace("\xa0", " ")
        text = text.replace("\u200b", "")
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        return text.strip()

    @classmethod
    def _one_line(cls, value):
        return re.sub(r"\s+", " ", cls._clean_text(value)).strip()

    @classmethod
    def _clean_multiline(cls, value):
        text = cls._clean_text(value)
        text = re.sub(r" *\n+ *", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def _node_text(cls, node):
        if node is None:
            return ""
        return cls._one_line(node.get_text(" ", strip=True))

    @classmethod
    def _parse_keywords(cls, value):
        if value is None:
            return []
        if isinstance(value, list):
            raw_values = value
        elif isinstance(value, str):
            raw_values = re.split(r"[,;\n]+", value)
        else:
            raw_values = [str(value)]
        return cls._dedupe(raw_values)

    @classmethod
    def _parse_people(cls, value):
        if not value:
            return []
        values = value if isinstance(value, list) else [value]
        names = []
        for item in values:
            if isinstance(item, dict):
                names.append(item.get("name") or item.get("@id") or "")
            else:
                names.append(str(item))
        return cls._dedupe(names)

    @staticmethod
    def _parse_org_name(value):
        if isinstance(value, dict):
            name = value.get("name")
            if name:
                return str(name).strip()
        if isinstance(value, str):
            return value.strip()
        return ""

    @staticmethod
    def _image_url(value):
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            url = value.get("url")
            if url:
                return str(url).strip()
        if isinstance(value, list):
            for item in value:
                url = MohGovGrArticlesCrawler._image_url(item)
                if url:
                    return url
        return ""

    @staticmethod
    def _article_id_from_html(raw_html):
        match = re.search(r'<article[^>]+id=["\']article_section_(\d+)["\']', raw_html or "")
        if match:
            return match.group(1)
        return ""

    @staticmethod
    def _external_id_from_url(url):
        path = urlparse(url or "").path
        match = re.search(r"/press-releases/(\d+)(?:-|/|$)", path)
        if match:
            return match.group(1)
        path = path.strip("/")
        if path:
            return path
        return url or ""

    @staticmethod
    def _is_press_detail_url(url):
        parsed = urlparse(url or "")
        host = parsed.netloc.lower()
        if host not in {"www.moh.gov.gr", "moh.gov.gr"}:
            return False
        return bool(re.match(r"^/articles/ministry/grafeio-typoy/press-releases/\d+", parsed.path))

    @staticmethod
    def _is_share_url(url):
        parsed = urlparse(url or "")
        return parsed.netloc.lower() in {
            "www.facebook.com",
            "facebook.com",
            "www.linkedin.com",
            "linkedin.com",
            "x.com",
            "twitter.com",
        }

    @staticmethod
    def _looks_like_pdf(url):
        return bool(re.search(r"\.pdf(?:[?#]|$)", url or "", flags=re.I))

    @staticmethod
    def _looks_like_attachment(url):
        return bool(
            re.search(r"\.(?:docx?|xlsx?|pptx?|zip|rar|odt|ods)(?:[?#]|$)", url or "", flags=re.I)
        )

    @classmethod
    def _dedupe(cls, values):
        seen = set()
        result = []
        for value in values:
            clean = cls._one_line(value)
            key = clean.lower()
            if clean and key not in seen:
                result.append(clean)
                seen.add(key)
        return result

    @staticmethod
    def _dedupe_dicts(items, key):
        seen = set()
        result = []
        for item in items:
            value = item.get(key)
            if not value or value in seen:
                continue
            result.append(item)
            seen.add(value)
        return result
