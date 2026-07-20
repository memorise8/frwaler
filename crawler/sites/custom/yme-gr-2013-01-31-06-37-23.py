# -*- coding: utf-8 -*-
"""Crawler for yme.gr press releases.

The category page is a Joomla/K2 listing. Its real list endpoint is the
category URL with ``format=json`` and ``start=N`` pagination. Individual
``item/...?...format=json`` URLs currently return HTTP 500, so detail records
are fetched from the public K2 item HTML pages and parsed from JSON-LD/K2 HTML.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class YmeGr20130131063723Crawler(BaseCrawler):
    site_id = "yme-gr-2013-01-31-06-37-23"
    site_name = "Custom: yme-gr-2013-01-31-06-37-23"
    base_url = "https://www.yme.gr"

    START_URL = (
        "https://www.yme.gr/2013-01-31-06-37-23/2013-01-31-07-00-49"
    )
    PAGE_SIZE = 14
    BACKOFF_SECONDS = (1, 3, 9)
    CURL_TIMEOUT = 45
    MIN_ABSTRACT_CHARS = 50
    DEPARTMENT = "Ministry of Infrastructure and Transport, Greece"
    _CURL_META_MARKER = "__YME_GR_CURL_META__:"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        start = 0
        seen = set()

        while True:
            if limit is not None and saved >= limit:
                break

            list_url = self._list_url(start)
            raw = self._curl_get(
                list_url,
                context=f"list endpoint start={start}",
                referer=self.START_URL,
                accept="application/json,text/html;q=0.9,*/*;q=0.8",
            )
            if not raw:
                print(f"[{self.site_id}] list endpoint failed at start={start}; stopping")
                break

            records = self._parse_list_response(raw, list_url)
            if not records:
                print(f"[{self.site_id}] no records found at start={start}; stopping")
                break

            print(
                f"[{self.site_id}] start={start}: discovered {len(records)} "
                "records from K2 category JSON endpoint"
            )

            non_duplicate_records = 0
            for idx, record in enumerate(records, start=1):
                if limit is not None and saved >= limit:
                    break

                item_label = record.get("external_id") or f"{start}.{idx}"
                try:
                    detail_url = record.get("url") or ""
                    if not detail_url:
                        raise RuntimeError("record has no detail URL")

                    dedupe_key = record.get("external_id") or detail_url
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    non_duplicate_records += 1

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
                            f"abstract shorter than {self.MIN_ABSTRACT_CHARS} chars "
                            f"({len(abstract)} chars)"
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
                        "doi": "",
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
            if non_duplicate_records == 0:
                print(f"[{self.site_id}] no new records at start={start}; stopping")
                break
            if len(records) < self.PAGE_SIZE:
                break
            start += len(records)

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
            "Accept: "
            + (
                accept
                or "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "application/json,*/*;q=0.8"
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
                stderr = (result.stderr or b"").decode(
                    "utf-8", errors="replace"
                ).strip()
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
        meta = raw[marker_pos + 1 + len(self._CURL_META_MARKER) :].strip()
        if "\t" not in meta:
            return body, "", fallback_url
        http_code, effective_url = meta.split("\t", 1)
        return body, http_code.strip(), effective_url.strip() or fallback_url

    # ------------------------------------------------------------------
    # List/detail parsing
    # ------------------------------------------------------------------

    def _list_url(self, start):
        if start <= 0:
            return f"{self.START_URL}?format=json"
        return f"{self.START_URL}?format=json&start={start}"

    def _parse_list_response(self, raw, list_url):
        try:
            data = json.loads(raw)
            items = data.get("items") or []
            if isinstance(items, list):
                return [r for r in (self._record_from_json_item(i, list_url) for i in items) if r]
        except (TypeError, ValueError) as exc:
            print(f"[{self.site_id}] list JSON parse failed, falling back to HTML: {exc}")

        soup = self._make_soup(raw, context=f"list page {list_url}")
        if soup is None:
            return []
        return self._parse_list_html(soup, list_url)

    def _record_from_json_item(self, item, list_url):
        if not isinstance(item, dict):
            return None

        external_id = self._one_line(item.get("id"))
        link = self._one_line(item.get("link"))
        url = urljoin(self.base_url, link) if link else ""
        if not external_id:
            external_id = self._external_id_from_url(url)
        title = self._one_line(item.get("title"))
        if not external_id or not url or not title:
            return None

        category = self._category_name(item.get("category")) or "Press releases"
        tags = self._tags_from_list(item.get("tags"))
        author_names = self._parse_people(item.get("author"))
        intro_html = item.get("introtext") or item.get("fulltext") or ""
        attachments = self._attachments_from_list_json(item.get("attachments"))

        return {
            "external_id": external_id,
            "title": title,
            "url": url,
            "published_date": self._parse_date(item.get("created")),
            "modified_date": self._parse_date(item.get("modified")),
            "category": category,
            "category_id": self._one_line(item.get("catid")),
            "authors": author_names,
            "keywords": tags,
            "abstract_hint": self._html_to_text(intro_html),
            "attachments": attachments,
            "list_endpoint": list_url,
            "list_raw": self._compact_list_metadata(item),
        }

    def _parse_list_html(self, soup, list_url):
        records = []
        for card in soup.select("#k2Container .catItemView"):
            link = card.select_one(".catItemTitle a[href]")
            if link is None:
                continue
            url = urljoin(self.base_url, link.get("href", "").strip())
            external_id = self._external_id_from_url(url)
            title = self._node_text(link)
            if not external_id or not url or not title:
                continue
            records.append(
                {
                    "external_id": external_id,
                    "title": title,
                    "url": url,
                    "published_date": self._parse_date(
                        self._node_text(card.select_one(".catItemDateCreated"))
                    ),
                    "modified_date": "",
                    "category": "Press releases",
                    "category_id": "",
                    "authors": self._parse_people(
                        self._node_text(card.select_one(".catItemAuthor"))
                    ),
                    "keywords": [],
                    "abstract_hint": self._extract_k2_card_text(card),
                    "attachments": [],
                    "list_endpoint": list_url,
                    "list_raw": {},
                }
            )
        return records

    def _parse_detail(self, soup, raw_html, record):
        article_data = self._find_article_jsonld(soup)
        canonical = (
            self._link_href(soup, "link[rel='canonical']")
            or self._meta_content(soup, "og:url", "twitter:url")
            or self._one_line(article_data.get("url"))
            or record.get("url", "")
        )
        url = urljoin(self.base_url, canonical)

        external_id = (
            self._external_id_from_url(url)
            or self._article_id_from_html(raw_html)
            or record.get("external_id", "")
        )
        if not external_id:
            raise RuntimeError("detail page has no external id")

        title = (
            self._one_line(article_data.get("headline"))
            or self._node_text(soup.select_one(".itemTitle"))
            or self._one_line(self._meta_content(soup, "og:title", "twitter:title"))
            or record.get("title", "")
        )
        if not title:
            raise RuntimeError("detail page has no title")

        abstract = (
            self._clean_multiline(article_data.get("articleBody"))
            or self._extract_k2_detail_text(soup)
            or self._clean_multiline(article_data.get("description"))
            or self._clean_multiline(self._meta_content(soup, "description", "og:description"))
            or record.get("abstract_hint", "")
        )

        published_date = (
            self._parse_date(article_data.get("datePublished"))
            or self._parse_date(self._node_text(soup.select_one(".itemBlogDate .sp_date_day")))
            or record.get("published_date", "")
        )
        modified_date = self._parse_date(article_data.get("dateModified")) or record.get(
            "modified_date", ""
        )

        authors = self._parse_people(article_data.get("author")) or record.get("authors", [])
        publisher = self._parse_org_name(article_data.get("publisher")) or self.DEPARTMENT
        category = record.get("category") or "Press releases"

        keywords = self._merge_unique(
            self._parse_keywords(article_data.get("keywords")),
            record.get("keywords", []),
            [category] if category else [],
        )

        attachments = self._merge_attachments(
            self._extract_detail_attachments(soup),
            record.get("attachments", []),
        )
        pdf_url = ""
        for attachment in attachments:
            if self._looks_like_pdf(attachment):
                pdf_url = attachment["url"]
                break

        image_urls = self._merge_unique(
            self._image_urls(article_data.get("image")),
            self._extract_image_urls(soup),
            [self._meta_content(soup, "og:image", "twitter:image")],
        )

        metadata = {
            "source": "Joomla/K2 category JSON list endpoint plus public K2 item HTML detail page",
            "list_endpoint": record.get("list_endpoint", self._list_url(0)),
            "detail_endpoint": "public item HTML page",
            "canonical_url": url,
            "content_id": external_id,
            "category_id": record.get("category_id", ""),
            "modified_date": modified_date,
            "json_ld_present": bool(article_data),
            "images": image_urls,
            "attachments": attachments,
            "list_record": record.get("list_raw", {}),
        }

        return {
            "external_id": external_id,
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "category": category,
            "keywords": keywords,
            "published_date": published_date,
            "url": url,
            "pdf_url": pdf_url,
            "department": publisher,
            "metadata": metadata,
        }

    # ------------------------------------------------------------------
    # Soup and text helpers
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

    def _html_to_text(self, html):
        if not html:
            return ""
        soup = self._make_soup(str(html), context="HTML fragment")
        if soup is None:
            return self._clean_multiline(re.sub(r"<[^>]+>", " ", str(html)))
        for bad in soup.select("script, style, noscript, iframe, svg"):
            bad.decompose()
        parts = []
        for node in soup.find_all(["p", "li", "h2", "h3", "h4"], recursive=True):
            text = self._node_text(node)
            if text and text not in parts:
                parts.append(text)
        if parts:
            return self._clean_multiline("\n\n".join(parts))
        return self._clean_multiline(soup.get_text("\n", strip=True))

    def _extract_k2_card_text(self, card):
        body = card.select_one(".catItemIntroText") or card.select_one(".catItemBody")
        if body is None:
            return ""
        return self._html_to_text(str(body))

    def _extract_k2_detail_text(self, soup):
        node = (
            soup.select_one(".itemFullText")
            or soup.select_one(".itemIntroText")
            or soup.select_one(".itemBody")
        )
        if node is None:
            return ""

        fragment = self._make_soup(str(node), context="detail article fragment")
        if fragment is None:
            return ""
        for bad in fragment.select(
            "script, style, noscript, iframe, svg, .itemLinks, "
            ".itemAttachmentsBlock, .itemVideoBlock, .sigFreeContainer, "
            ".itemRatingBlock, .itemAuthorBlock, h2.itemTitle"
        ):
            bad.decompose()
        return self._html_to_text(str(fragment))

    @staticmethod
    def _clean_multiline(value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\xa0", " ").replace("&nbsp;", " ")
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
        text = re.sub(r"\r\n?", "\n", text)
        text = re.sub(r"[ \t\f\v]+", " ", text)
        text = re.sub(r" *\n *", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def _one_line(cls, value):
        return re.sub(r"\s+", " ", cls._clean_multiline(value)).strip()

    @classmethod
    def _node_text(cls, node):
        if node is None:
            return ""
        return cls._one_line(node.get_text(" ", strip=True))

    # ------------------------------------------------------------------
    # Structured data helpers
    # ------------------------------------------------------------------

    def _find_article_jsonld(self, soup):
        for obj in self._iter_jsonld(soup):
            if not isinstance(obj, dict):
                continue
            types = obj.get("@type")
            if isinstance(types, str):
                types = [types]
            if not isinstance(types, list):
                types = []
            if any(t in {"Article", "NewsArticle", "BlogPosting"} for t in types):
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

    @staticmethod
    def _link_href(soup, selector):
        node = soup.select_one(selector)
        if node and node.get("href"):
            return node.get("href", "").strip()
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

    # ------------------------------------------------------------------
    # Field normalization
    # ------------------------------------------------------------------

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

        greek_months = {
            "\u0399\u03b1\u03bd\u03bf\u03c5\u03b1\u03c1\u03af\u03bf\u03c5": 1,
            "\u03a6\u03b5\u03b2\u03c1\u03bf\u03c5\u03b1\u03c1\u03af\u03bf\u03c5": 2,
            "\u039c\u03b1\u03c1\u03c4\u03af\u03bf\u03c5": 3,
            "\u0391\u03c0\u03c1\u03b9\u03bb\u03af\u03bf\u03c5": 4,
            "\u039c\u03b1\u0390\u03bf\u03c5": 5,
            "\u039c\u03b1\u03ca\u03bf\u03c5": 5,
            "\u0399\u03bf\u03c5\u03bd\u03af\u03bf\u03c5": 6,
            "\u0399\u03bf\u03c5\u03bb\u03af\u03bf\u03c5": 7,
            "\u0391\u03c5\u03b3\u03bf\u03cd\u03c3\u03c4\u03bf\u03c5": 8,
            "\u03a3\u03b5\u03c0\u03c4\u03b5\u03bc\u03b2\u03c1\u03af\u03bf\u03c5": 9,
            "\u039f\u03ba\u03c4\u03c9\u03b2\u03c1\u03af\u03bf\u03c5": 10,
            "\u039d\u03bf\u03b5\u03bc\u03b2\u03c1\u03af\u03bf\u03c5": 11,
            "\u0394\u03b5\u03ba\u03b5\u03bc\u03b2\u03c1\u03af\u03bf\u03c5": 12,
        }
        match = re.search(r"\b(\d{1,2})\s+([^\W\d_]+)\s+((?:19|20)\d{2})\b", text)
        if match:
            month = greek_months.get(match.group(2))
            if month:
                return f"{int(match.group(3)):04d}-{month:02d}-{int(match.group(1)):02d}"

        try:
            return parsedate_to_datetime(text).date().isoformat()
        except (TypeError, ValueError, IndexError, OverflowError):
            return ""

    @staticmethod
    def _external_id_from_url(url):
        match = re.search(r"/item/(\d+)(?:-|/|$)", url or "")
        if match:
            return match.group(1)
        return ""

    @staticmethod
    def _article_id_from_html(raw_html):
        match = re.search(r'startOfPageId(\d+)', raw_html or "")
        if match:
            return match.group(1)
        match = re.search(r'"id"\s*:\s*"(\d+)"', raw_html or "")
        if match:
            return match.group(1)
        return ""

    def _parse_people(self, value):
        if not value:
            return []
        if isinstance(value, list):
            people = []
            for item in value:
                people.extend(self._parse_people(item))
            return self._merge_unique(people)
        if isinstance(value, dict):
            name = self._one_line(value.get("name") or value.get("title") or value.get("email"))
            return [name] if name else []
        text = self._one_line(value)
        if not text:
            return []
        text = re.sub(r"^\s*Written by\s+", "", text, flags=re.IGNORECASE)
        return [text] if text else []

    def _parse_org_name(self, value):
        if isinstance(value, dict):
            return self._one_line(value.get("name") or value.get("legalName"))
        if isinstance(value, list):
            for item in value:
                name = self._parse_org_name(item)
                if name:
                    return name
        return self._one_line(value)

    def _parse_keywords(self, value):
        if not value:
            return []
        if isinstance(value, list):
            words = []
            for item in value:
                words.extend(self._parse_keywords(item))
            return self._merge_unique(words)
        text = self._one_line(value)
        if not text:
            return []
        return self._merge_unique([p.strip() for p in re.split(r"[,;|]", text) if p.strip()])

    def _tags_from_list(self, tags):
        if not isinstance(tags, list):
            return []
        words = []
        for tag in tags:
            if isinstance(tag, dict):
                label = self._one_line(tag.get("name") or tag.get("title"))
            else:
                label = self._one_line(tag)
            if label:
                words.append(label)
        return self._merge_unique(words)

    def _category_name(self, category):
        if isinstance(category, dict):
            return self._one_line(category.get("name") or category.get("title"))
        return self._one_line(category)

    @staticmethod
    def _merge_unique(*groups):
        merged = []
        seen = set()
        for group in groups:
            if group is None:
                continue
            if isinstance(group, str):
                iterable = [group]
            else:
                iterable = group
            for item in iterable:
                text = str(item or "").strip()
                if not text:
                    continue
                key = text.casefold()
                if key in seen:
                    continue
                seen.add(key)
                merged.append(text)
        return merged

    # ------------------------------------------------------------------
    # Attachments and media
    # ------------------------------------------------------------------

    def _attachments_from_list_json(self, attachments):
        if not attachments:
            return []
        if isinstance(attachments, dict):
            attachments = [attachments]
        if not isinstance(attachments, list):
            return []
        parsed = []
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            url = (
                attachment.get("url")
                or attachment.get("href")
                or attachment.get("link")
                or attachment.get("downloadLink")
            )
            label = (
                attachment.get("title")
                or attachment.get("name")
                or attachment.get("filename")
                or attachment.get("fileName")
            )
            if url:
                parsed.append(
                    {
                        "label": self._one_line(label),
                        "url": urljoin(self.base_url, self._one_line(url)),
                        "title": self._one_line(attachment.get("title")),
                    }
                )
        return self._dedupe_attachments(parsed)

    def _extract_detail_attachments(self, soup):
        attachments = []
        for link in soup.select(".itemAttachments a[href], .itemAttachmentsBlock a[href]"):
            href = urljoin(self.base_url, link.get("href", "").strip())
            if not href:
                continue
            attachments.append(
                {
                    "label": self._node_text(link),
                    "url": href,
                    "title": self._one_line(link.get("title")),
                }
            )
        return self._dedupe_attachments(attachments)

    def _merge_attachments(self, *groups):
        merged = []
        for group in groups:
            if group:
                merged.extend(group)
        return self._dedupe_attachments(merged)

    @staticmethod
    def _dedupe_attachments(attachments):
        seen = set()
        out = []
        for attachment in attachments:
            url = (attachment.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            out.append(attachment)
        return out

    @staticmethod
    def _looks_like_pdf(attachment):
        text = " ".join(
            [
                attachment.get("url", ""),
                attachment.get("label", ""),
                attachment.get("title", ""),
            ]
        ).lower()
        return ".pdf" in text or "pdf" in text

    def _image_urls(self, value):
        if not value:
            return []
        if isinstance(value, str):
            return [urljoin(self.base_url, value)] if value.strip() else []
        if isinstance(value, dict):
            url = value.get("url") or value.get("contentUrl")
            return [urljoin(self.base_url, url)] if url else []
        if isinstance(value, list):
            urls = []
            for item in value:
                urls.extend(self._image_urls(item))
            return urls
        return []

    def _extract_image_urls(self, soup):
        urls = []
        for img in soup.select(".itemFullText img[src], .itemIntroText img[src], .itemImage img[src]"):
            src = img.get("src", "").strip()
            if src:
                urls.append(urljoin(self.base_url, src))
        for link in soup.select(".sigFreeContainer a[href]"):
            href = link.get("href", "").strip()
            if href:
                urls.append(urljoin(self.base_url, href))
        return self._merge_unique(urls)

    def _compact_list_metadata(self, item):
        keys = [
            "id",
            "alias",
            "link",
            "catid",
            "created",
            "modified",
            "featured",
            "hits",
            "language",
        ]
        meta = {k: item.get(k) for k in keys if item.get(k) not in (None, "")}
        category = item.get("category")
        if isinstance(category, dict):
            meta["category"] = {
                k: category.get(k)
                for k in ("id", "name", "alias", "link")
                if category.get(k) not in (None, "")
            }
        author = item.get("author")
        if isinstance(author, dict):
            meta["author"] = {
                k: author.get(k)
                for k in ("name", "link")
                if author.get(k) not in (None, "")
            }
        return meta
