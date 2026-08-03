# -*- coding: utf-8 -*-
"""Crawler for NIDDK news-release archive records.

Starting URL: https://www.niddk.nih.gov/news/archive?nt=nr

The archive is rendered as HTML by the NIDDK site. Page 1 is fetched without a
``p`` parameter; page 2+ use ``p=N``. Detail URLs are listed in the HTML as
``a[data-gtm="news-item"]``. Recent RSS items expose Sitecore GUIDs, so the RSS
feed is used only as optional enrichment for native IDs/descriptions.
"""

from __future__ import annotations

import email.utils
import json
import os
import re
import subprocess
import sys
import time
import uuid
from html import unescape
from urllib.parse import unquote, urljoin, urlparse
from xml.etree import ElementTree

# Absolute import: spec_from_file_location gives this module no package context.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
from crawler.base_crawler import BaseCrawler  # noqa: E402


class NiddkNihGovNewsCrawler(BaseCrawler):
    site_id = "niddk-nih-gov-news"
    site_name = "Custom: niddk-nih-gov-news"
    base_url = "https://www.niddk.nih.gov"

    START_URL = "https://www.niddk.nih.gov/news/archive?nt=nr"
    LIST_ENDPOINT = "https://www.niddk.nih.gov/news/archive?nt=nr&p={page}"
    RSS_ENDPOINT = "https://www.niddk.nih.gov/rss/news"
    CATEGORY = "News Release"
    PUBLISHER = "National Institute of Diabetes and Digestive and Kidney Diseases; National Institutes of Health"
    DEPARTMENT = "National Institute of Diabetes and Digestive and Kidney Diseases"

    MAX_PAGES = 200
    MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    TIME_MARGIN_SECONDS = 30
    MIN_ABSTRACT_CHARS = 50
    RETRY_WAITS = (1, 3, 9)

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url, *, context="request", timeout=45):
        """Fetch a URL through curl with TLS settings and bounded retries."""
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--connect-timeout",
            "15",
            "--max-time",
            str(timeout),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            "Accept-Language: en-US,en;q=0.9",
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/rss+xml;q=0.8,*/*;q=0.7",
            url,
        ]

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
                text = result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                if result.returncode == 0 and text.strip():
                    return text
                if result.returncode == 0:
                    last_error = "empty response"
                else:
                    last_error = f"exit={result.returncode} stderr={stderr[:400]}"

            if attempt < 3:
                wait = self.RETRY_WAITS[attempt - 1]
                print(
                    f"[{self.site_id}] {context} curl failed "
                    f"{attempt}/3 for {url}: {last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None

    def _list_url(self, page):
        if page == 1:
            return self.START_URL
        return self.LIST_ENDPOINT.format(page=page)

    # ------------------------------------------------------------------
    # Text and parsing helpers
    # ------------------------------------------------------------------

    def _parse_html(self, raw, *, context="html"):
        try:
            from bs4 import BeautifulSoup
        except Exception as exc:
            print(f"[{self.site_id}] BeautifulSoup import failed for {context}: {exc}")
            return None

        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else (raw or "")
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
    def _meta_content(soup, *names):
        if soup is None:
            return ""
        for name in names:
            node = soup.find("meta", attrs={"name": name})
            if node and node.get("content"):
                return node.get("content", "").strip()
            node = soup.find("meta", attrs={"property": name})
            if node and node.get("content"):
                return node.get("content", "").strip()
        return ""

    @classmethod
    def _split_keywords(cls, *values):
        keywords = []
        for raw in values:
            if not raw:
                continue
            if isinstance(raw, (list, tuple, set)):
                pieces = raw
            else:
                pieces = re.split(r"\s*[,;|]\s*", str(raw))
            for piece in pieces:
                value = cls._one_line(piece)
                if value and value not in keywords:
                    keywords.append(value)
        return keywords

    @classmethod
    def _parse_date(cls, raw):
        raw = cls._one_line(raw)
        if not raw:
            return ""

        match = re.search(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b", raw)
        if match:
            return match.group(0)

        try:
            parsed = email.utils.parsedate_to_datetime(raw)
            if parsed:
                return parsed.date().isoformat()
        except Exception:
            pass

        raw = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", raw, flags=re.I)
        raw = raw.replace("Sept.", "Sep.").replace("Sept ", "Sep ")
        for fmt in (
            "%B %d, %Y",
            "%b. %d, %Y",
            "%b %d, %Y",
            "%A, %B %d, %Y",
            "%m/%d/%Y",
        ):
            try:
                return time.strftime("%Y-%m-%d", time.strptime(raw, fmt))
            except ValueError:
                continue
        return ""

    @staticmethod
    def _filename_from_url(url):
        if not url:
            return None
        parsed = urlparse(url)
        tail = unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1])
        if "." in tail and len(tail) <= 200:
            return tail
        return None

    @staticmethod
    def _slug_from_url(url):
        parsed = urlparse(url)
        path = parsed.path.strip("/")
        slug = path.rsplit("/", 1)[-1] if path else ""
        if not slug and parsed.fragment:
            slug = parsed.fragment.rsplit("/", 1)[-1]
        if not slug:
            slug = re.sub(r"[^a-zA-Z0-9]+", "-", url).strip("-")[-120:]
        return slug or url

    @staticmethod
    def _uuidish(value):
        if not value:
            return ""
        match = re.search(
            r"\{?([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\}?",
            str(value),
        )
        return match.group(1).lower() if match else ""

    @staticmethod
    def _extract_doi(*values):
        for value in values:
            if not value:
                continue
            match = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", str(value), flags=re.I)
            if match:
                return match.group(0).rstrip(".,);")
        return None

    # ------------------------------------------------------------------
    # List, RSS, and detail parsing
    # ------------------------------------------------------------------

    def _load_rss_index(self):
        raw = self._curl_get(self.RSS_ENDPOINT, context="rss index", timeout=30)
        if not raw:
            return {}
        try:
            root = ElementTree.fromstring(raw.encode("utf-8"))
        except Exception as exc:
            print(f"[{self.site_id}] RSS parse failed: {exc}")
            return {}

        index = {}
        for item in root.findall("./channel/item"):
            link = self._one_line(item.findtext("link"))
            if not link:
                continue
            guid_raw = self._one_line(item.findtext("guid"))
            pub_raw = self._one_line(item.findtext("pubDate"))
            index[link] = {
                "guid_raw": guid_raw,
                "guid": self._uuidish(guid_raw) or guid_raw,
                "title": self._one_line(item.findtext("title")),
                "description": self._clean_text(item.findtext("description")),
                "pubDate": pub_raw,
                "published_date": self._parse_date(pub_raw),
            }
        return index

    def _parse_list_page(self, raw, page, source_url):
        soup = self._parse_html(raw, context=f"list page {page}")
        if soup is None:
            return [], False

        items = []
        for row_index, row in enumerate(soup.select("ol.result-items.news > li"), start=1):
            link = row.select_one('a[data-gtm="news-item"][href]')
            if link is None:
                continue

            href = (link.get("href") or "").strip()
            url = urljoin(self.base_url, href)
            title = self._one_line(link.get_text(" ", strip=True))
            if not url or not title:
                continue

            time_node = row.find("time")
            listed_date_raw = ""
            listed_date = ""
            if time_node is not None:
                listed_date_raw = self._one_line(time_node.get_text(" ", strip=True))
                listed_date = self._parse_date(time_node.get("datetime") or listed_date_raw)

            category_node = row.select_one(".news-type")
            category = self._one_line(category_node.get_text(" ", strip=True)) if category_node else self.CATEGORY

            abstract_node = row.find("p")
            abstract = self._clean_text(abstract_node.get_text(" ", strip=True) if abstract_node else "")
            items.append(
                {
                    "title": title,
                    "url": url,
                    "listed_date_raw": listed_date_raw,
                    "listed_date": listed_date,
                    "category": category or self.CATEGORY,
                    "abstract": abstract,
                    "source_page": source_url,
                    "page": page,
                    "row_index": row_index,
                    "href": href,
                }
            )

        has_next = bool(
            soup.select_one('.dk-pagination button[title="Next page"], .dk-pagination button[aria-label="Next page"]')
            or soup.find("a", rel="next")
        )
        return items, has_next

    def _parse_json_ld(self, soup):
        records = []
        if soup is None:
            return records
        for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = node.string or node.get_text("", strip=True)
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except Exception:
                continue
            if isinstance(parsed, list):
                records.extend([obj for obj in parsed if isinstance(obj, dict)])
            elif isinstance(parsed, dict):
                graph = parsed.get("@graph")
                if isinstance(graph, list):
                    records.extend([obj for obj in graph if isinstance(obj, dict)])
                records.append(parsed)
        return records

    def _extract_datalayer(self, raw):
        match = re.search(r"dataLayer\.push\((\{.*?\})\);", raw or "", flags=re.S)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(1))
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def _article_text(self, soup):
        if soup is None:
            return ""
        for selector in (
            "article.dk-content",
            "article .field--name-body",
            "article .field__item",
            "main article",
            "article",
            "main",
        ):
            node = soup.select_one(selector)
            if node is None:
                continue
            for junk in node.select("script, style, nav, aside, form, .dk-share, .share, .breadcrumb, .usa-breadcrumb"):
                junk.decompose()
            text = self._clean_text(node.get_text("\n", strip=True))
            if len(text) >= self.MIN_ABSTRACT_CHARS:
                return text[:8000]
        return ""

    def _find_pdf_url(self, soup, page_url):
        if soup is None:
            return None
        for link in soup.find_all("a", href=True):
            href = link.get("href", "").strip()
            if re.search(r"\.pdf(?:[?#]|$)", href, flags=re.I):
                return urljoin(page_url, href)
        return None

    def _parse_detail_page(self, raw, url):
        soup = self._parse_html(raw, context=f"detail {url}")
        if soup is None:
            return {"detail_parse_status": "parse_failed"}

        title_text = self._one_line(soup.title.get_text(" ", strip=True) if soup.title else "")
        blocked = "Just a moment" in title_text or bool(soup.select_one("#challenge-error-text"))

        data_layer = self._extract_datalayer(raw)
        json_ld = self._parse_json_ld(soup)
        json_ld_record = {}
        for record in json_ld:
            if any(record.get(key) for key in ("datePublished", "dateModified", "headline", "description")):
                json_ld_record = record
                break

        canonical_node = soup.find("link", rel=lambda value: value and "canonical" in value)
        canonical_url = canonical_node.get("href", "").strip() if canonical_node else ""
        if canonical_url:
            canonical_url = urljoin(url, canonical_url)

        h1 = soup.find("h1")
        detail_title = (
            self._one_line(json_ld_record.get("headline"))
            or self._meta_content(soup, "og:title", "twitter:title")
            or self._one_line(h1.get_text(" ", strip=True) if h1 else "")
        )

        published_raw = ""
        time_node = soup.select_one('time[itemprop="datePublished"], time[datetime]')
        if time_node is not None:
            published_raw = self._one_line(time_node.get("datetime") or time_node.get_text(" ", strip=True))
        published_raw = (
            self._one_line(json_ld_record.get("datePublished"))
            or published_raw
            or self._meta_content(soup, "article:published_time", "citation_publication_date", "date")
        )

        modified_raw = (
            self._one_line(json_ld_record.get("dateModified"))
            or self._meta_content(soup, "article:modified_time", "last-modified")
            or self._one_line(data_layer.get("lastReviewedDate"))
        )

        meta_description = (
            self._one_line(json_ld_record.get("description"))
            or self._meta_content(soup, "description", "og:description", "twitter:description")
        )
        body = "" if blocked else self._article_text(soup)
        abstract = body if len(body) > len(meta_description) else meta_description

        authors = []
        for node in soup.find_all("meta", attrs={"name": "citation_author"}):
            author = self._one_line(node.get("content"))
            if author and author not in authors:
                authors.append(author)

        keywords = self._split_keywords(
            self._meta_content(soup, "keywords", "news_keywords"),
            data_layer.get("pageDiseaseArea"),
        )
        pdf_url = self._find_pdf_url(soup, url)
        raw_for_doi = "\n".join([raw[:20000] if raw else "", abstract or "", meta_description or ""])

        return {
            "detail_parse_status": "blocked" if blocked else "ok",
            "canonical_url": canonical_url,
            "title": detail_title,
            "abstract": abstract,
            "published_date_raw": published_raw,
            "published_date": self._parse_date(published_raw),
            "modified_date_raw": modified_raw,
            "modified_date": self._parse_date(modified_raw),
            "authors": authors,
            "keywords": keywords,
            "pdf_url": pdf_url,
            "doi": self._extract_doi(raw_for_doi),
            "node_id": self._uuidish(data_layer.get("pageID")) or self._uuidish(json.dumps(json_ld_record)),
            "pageID": data_layer.get("pageID"),
            "pagePath": data_layer.get("pagePath"),
            "pageDiseaseArea": data_layer.get("pageDiseaseArea"),
            "lastReviewedDate": data_layer.get("lastReviewedDate"),
            "json_ld": json_ld_record,
        }

    def _build_paper(self, item, detail, rss_item):
        detail = detail or {}
        rss_item = rss_item or {}

        detail_url = item.get("url")
        slug = self._slug_from_url(detail_url)
        node_id = detail.get("node_id") or self._uuidish(rss_item.get("guid"))
        rss_guid = self._uuidish(rss_item.get("guid")) or rss_item.get("guid") or ""
        external_id = rss_guid or node_id or slug
        post_number = node_id or rss_guid or slug or None

        # The NIDDK list page is the curated source of truth for the headline;
        # detail pages are cross-domain and sometimes resolve to a generic
        # shell title (e.g. archive.cdc.gov SPA shells return "CDC Newsroom").
        title = item.get("title") or self._one_line(detail.get("title")) or rss_item.get("title") or ""
        listed_date = item.get("listed_date") or self._parse_date(rss_item.get("pubDate"))
        listed_raw = item.get("listed_date_raw") or rss_item.get("pubDate") or listed_date
        published_date = detail.get("published_date") or rss_item.get("published_date") or listed_date

        list_abstract = item.get("abstract") or ""
        rss_abstract = rss_item.get("description") or ""
        detail_abstract = detail.get("abstract") or ""
        abstract = max(
            [detail_abstract, list_abstract, rss_abstract],
            key=lambda value: len(value or ""),
        )
        abstract = self._clean_text(abstract)

        category = item.get("category") or self.CATEGORY
        keywords = self._split_keywords(category, detail.get("keywords"))
        journal = None
        journal_raw = None
        pdf_url = detail.get("pdf_url")
        original_filename = self._filename_from_url(pdf_url)
        doi = detail.get("doi") or self._extract_doi(abstract, title)

        metadata = {
            "source": "NIDDK archive HTML",
            "start_url": self.START_URL,
            "list_endpoint": item.get("source_page"),
            "detail_endpoint": detail_url,
            "rss_endpoint": self.RSS_ENDPOINT if rss_item else None,
            "posted_date": listed_raw,
            "listed_date": listed_date,
            "originalFilename": original_filename,
            "journal_raw": journal_raw,
            "series": None,
            "volume": None,
            "issue": None,
            "node_id": node_id,
            "pageID": detail.get("pageID"),
            "pagePath": detail.get("pagePath"),
            "rss_guid": rss_guid,
            "rss_guid_raw": rss_item.get("guid_raw"),
            "rss_pubDate": rss_item.get("pubDate"),
            "post_number": post_number,
            "slug": slug,
            "category": category,
            "page": item.get("page"),
            "row_index": item.get("row_index"),
            "href": item.get("href"),
            "detail_parse_status": detail.get("detail_parse_status"),
            "canonical_url": detail.get("canonical_url"),
            "published_date_raw": detail.get("published_date_raw"),
            "modified_date": detail.get("modified_date"),
            "modified_date_raw": detail.get("modified_date_raw"),
            "lastReviewedDate": detail.get("lastReviewedDate"),
            "pageDiseaseArea": detail.get("pageDiseaseArea"),
            "raw_list_record": item,
            "raw_rss_record": rss_item or None,
            "raw_detail_record": {
                key: detail.get(key)
                for key in (
                    "node_id",
                    "pageID",
                    "pagePath",
                    "pageDiseaseArea",
                    "lastReviewedDate",
                    "json_ld",
                )
                if detail.get(key) not in (None, "", [], {})
            },
        }

        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.site_id}:{external_id}")),
            "site_id": self.site_id,
            "external_id": str(external_id),
            "post_number": str(post_number) if post_number is not None else None,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": "; ".join(detail.get("authors") or []),
            "publisher": self.PUBLISHER,
            "department": self.DEPARTMENT,
            "journal": journal,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": ", ".join(keywords),
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str),
        }

    def _time_budget_exhausted(self, started_at):
        return time.monotonic() - started_at >= self.MAX_SECONDS - self.TIME_MARGIN_SECONDS

    # ------------------------------------------------------------------
    # Crawl entry point
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        started_at = time.monotonic()
        limit_or_inf = limit if limit is not None else "inf"

        rss_index = self._load_rss_index()

        for page in range(1, self.MAX_PAGES + 1):
            if self._time_budget_exhausted(started_at):
                print(f"[{self.site_id}] approaching 25-minute crawl budget at page {page}; exiting cleanly")
                break
            if limit is not None and saved >= limit:
                break

            list_url = self._list_url(page)
            raw = self._curl_get(list_url, context=f"list page {page}")
            if not raw:
                print(f"[{self.site_id}] page {page}: fetch failed; stopping")
                break

            try:
                items, has_next = self._parse_list_page(raw, page, list_url)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] page {page}: parse failed: {exc}; stopping")
                break

            if not items:
                print(f"[{self.site_id}] page {page}: 0 records; stopping")
                break

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break
                if self._time_budget_exhausted(started_at):
                    print(f"[{self.site_id}] approaching 25-minute crawl budget during page {page}; exiting cleanly")
                    return saved

                detail_url = item.get("url")
                item_label = item.get("title") or detail_url or "unknown"
                if not detail_url:
                    continue
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                try:
                    time.sleep(self.detail_delay)
                    detail_raw = self._curl_get(detail_url, context=f"detail {item_label[:80]}")
                    if not detail_raw:
                        print(f"[{self.site_id}] item {item_label[:80]} failed: detail fetch failed after retries")
                        continue

                    detail = self._parse_detail_page(detail_raw, detail_url)
                    paper = self._build_paper(item, detail, rss_index.get(detail_url))
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(f"[{self.site_id}] item {item_label[:80]} skipped: abstract too short ({len(abstract)} chars)")
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:80]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label[:80]} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: no new URLs; stopping")
                break
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")
            if not has_next:
                print(f"[{self.site_id}] page {page}: next page absent; stopping")
                break
        else:
            print(f"[{self.site_id}] reached safety cap of {self.MAX_PAGES} pages")

        print(f"[{self.site_id}] crawl complete: {saved} saved")
        return saved
