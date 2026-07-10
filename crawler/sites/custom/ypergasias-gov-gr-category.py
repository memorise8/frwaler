# -*- coding: utf-8 -*-
"""Crawler for ypergasias.gov.gr press-release category.

The category page is a WordPress archive. The real list endpoint is the public
WordPress REST API category query:

    /wp-json/wp/v2/posts?categories=4

Each post is then enriched via its detail API endpoint:

    /wp-json/wp/v2/posts/{id}?_embed=1

Category ID 4 corresponds to /category/anakoinoseis-typou/ (Ανακοινώσεις Τύπου).
Total posts: ~3146 across ~1049 pages as of 2026-05.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class YpergasiasGovGrCategoryCrawler(BaseCrawler):
    site_id = "ypergasias-gov-gr-category"
    site_name = "Custom: ypergasias-gov-gr-category"
    base_url = "https://ypergasias.gov.gr"

    START_URL = "https://ypergasias.gov.gr/category/anakoinoseis-typou/"
    LIST_API_URL = "https://ypergasias.gov.gr/wp-json/wp/v2/posts"
    DETAIL_API_URL = "https://ypergasias.gov.gr/wp-json/wp/v2/posts/{post_id}"
    CATEGORY_ID = 4
    PAGE_SIZE = 25
    BACKOFF_SECONDS = (1, 3, 9)
    CURL_TIMEOUT = 45
    MIN_ABSTRACT_CHARS = 50
    MIN_SAVED_ABSTRACT_CHARS = 100
    MAX_PAGES = 200
    WALL_CLOCK_BUDGET_SECS = 25 * 60
    DEPARTMENT = "Ministry of Labour and Social Insurance, Greece"
    _CURL_META_MARKER = "__YPERGASIAS_GOV_GR_CURL_META__:"
    # The WAF on this site blocks the default Chrome UA; Firefox UA works fine.
    CURL_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls = set()
        start_time = time.time()

        print(
            f"[{self.site_id}] using list API endpoint: "
            f"{self.LIST_API_URL}?categories={self.CATEGORY_ID}"
        )

        while True:
            if limit is not None and saved >= limit:
                break

            if page > self.MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {self.MAX_PAGES} pages reached; stopping")
                break

            elapsed = time.time() - start_time
            if elapsed >= self.WALL_CLOCK_BUDGET_SECS:
                print(f"[{self.site_id}] wall-clock budget exceeded ({elapsed:.0f}s); stopping")
                break

            list_url = self._list_api_url(page)
            raw = self._curl_get(
                list_url,
                context=f"list API page {page}",
                referer=self.START_URL,
                accept="application/json,text/html;q=0.9,*/*;q=0.8",
            )
            if not raw:
                print(f"[{self.site_id}] list endpoint failed at page {page}; stopping")
                break

            records = self._parse_list_response(raw, list_url)
            if not records:
                print(f"[{self.site_id}] no records found at page {page}; stopping")
                break

            if page % 10 == 0:
                limit_str = str(limit) if limit is not None else "inf"
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            for idx, record in enumerate(records, start=1):
                if limit is not None and saved >= limit:
                    break

                item_label = record.get("external_id") or f"{page}.{idx}"
                try:
                    dedupe_key = record.get("url") or record.get("external_id")
                    if dedupe_key and dedupe_key in seen_urls:
                        continue
                    if dedupe_key:
                        seen_urls.add(dedupe_key)

                    detail_url = self._detail_api_url(record.get("external_id"))
                    if not detail_url:
                        raise RuntimeError("record has no detail API URL")

                    time.sleep(self.detail_delay)
                    detail_raw = self._curl_get(
                        detail_url,
                        context=f"item {item_label} detail API",
                        referer=list_url,
                        accept="application/json,text/html;q=0.9,*/*;q=0.8",
                    )
                    if not detail_raw:
                        raise RuntimeError("detail fetch failed after retries")

                    try:
                        detail_data = json.loads(detail_raw)
                    except (TypeError, ValueError) as exc:
                        print(
                            f"[{self.site_id}] item {item_label} detail JSON "
                            f"parse failed, trying public HTML: {exc}"
                        )
                        parsed = self._parse_public_detail_fallback(record, list_url)
                    else:
                        if not isinstance(detail_data, dict):
                            raise RuntimeError("detail API returned non-object JSON")
                        parsed = self._parse_detail_json(detail_data, record, detail_url)

                    abstract = parsed.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
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
                        "post_number": parsed["external_id"],
                        "title": parsed["title"],
                        "authors": json.dumps(parsed["authors"], ensure_ascii=False),
                        "abstract": abstract,
                        "category": parsed["category"],
                        "keywords": json.dumps(parsed["keywords"], ensure_ascii=False),
                        "published_date": parsed["published_date"],
                        "listed_date": parsed.get("listed_date") or parsed["published_date"],
                        "url": parsed["url"],
                        "pdf_url": parsed["pdf_url"],
                        "doi": "",
                        "department": parsed["department"],
                        "publisher": parsed.get("publisher") or self.DEPARTMENT,
                        "metadata": json.dumps(parsed["metadata"], ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {parsed['title'][:90]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break
            if len(records) < self.PAGE_SIZE:
                print(f"[{self.site_id}] page {page} returned {len(records)} < {self.PAGE_SIZE}; last page reached")
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
            "--tls-max", "1.3",
            "-skL",
            "--compressed",
            "--connect-timeout", "15",
            "--max-time", str(self.CURL_TIMEOUT),
            "-A", self.CURL_USER_AGENT,
            "-H", "Accept: " + (
                accept or "text/html,application/xhtml+xml,application/xml;q=0.9,"
                          "application/json,*/*;q=0.8"
            ),
            "-H", "Accept-Language: el-GR,el;q=0.9,en;q=0.7",
            "-w", "\n" + self._CURL_META_MARKER + "%{http_code}\t%{url_effective}",
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
    # List / detail parsing
    # ------------------------------------------------------------------

    def _list_api_url(self, page):
        params = {
            "categories": str(self.CATEGORY_ID),
            "per_page": str(self.PAGE_SIZE),
            "page": str(page),
            "_fields": (
                "id,date,date_gmt,modified,modified_gmt,slug,link,title,"
                "excerpt,categories,tags"
            ),
        }
        return f"{self.LIST_API_URL}?{urlencode(params)}"

    def _detail_api_url(self, post_id):
        if not post_id:
            return ""
        return f"{self.DETAIL_API_URL.format(post_id=post_id)}?_embed=1"

    def _parse_list_response(self, raw, list_url):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError) as exc:
            print(f"[{self.site_id}] list JSON parse failed, falling back to HTML: {exc}")
            soup = self._make_soup(raw, context=f"list page {list_url}")
            if soup is None:
                return []
            return self._parse_list_html(soup, list_url)

        if not isinstance(data, list):
            print(f"[{self.site_id}] list API returned non-list JSON")
            return []

        records = []
        for item in data:
            record = self._record_from_post(item, list_url)
            if record:
                records.append(record)
        return records

    def _record_from_post(self, item, list_url):
        if not isinstance(item, dict):
            return None
        post_id = item.get("id")
        if post_id in (None, ""):
            return None
        post_id = str(post_id)
        link = item.get("link") or ""
        title = self._clean_title(self._rendered(item.get("title")))
        return {
            "external_id": post_id,
            "title": title,
            "url": link,
            "published_date": self._parse_date(item.get("date")),
            "list_api_url": list_url,
            "list_excerpt": self._extract_fragment_text(
                self._rendered(item.get("excerpt")),
                context=f"list excerpt {post_id}",
            ),
            "raw_list": {
                "id": item.get("id"),
                "date": item.get("date"),
                "date_gmt": item.get("date_gmt"),
                "modified": item.get("modified"),
                "modified_gmt": item.get("modified_gmt"),
                "slug": item.get("slug"),
                "categories": item.get("categories") or [],
                "tags": item.get("tags") or [],
            },
        }

    def _parse_list_html(self, soup, list_url):
        records = []
        seen = set()
        for article in soup.select("article[id^='post-']"):
            link_node = (
                article.select_one(".entry-title a[href]")
                or article.select_one("a[rel='bookmark'][href]")
            )
            if link_node is None:
                continue
            url = urljoin(self.base_url, link_node.get("href", "").strip())
            if not url or url in seen:
                continue
            seen.add(url)
            match = re.search(r"post-(\d+)", article.get("id", "") or "")
            post_id = match.group(1) if match else self._external_id_from_url(url)
            title = self._clean_title(self._node_text(link_node))
            if not post_id or not title:
                continue
            records.append({
                "external_id": post_id,
                "title": title,
                "url": url,
                "published_date": "",
                "list_api_url": list_url,
                "list_excerpt": "",
                "raw_list": {"source": "category HTML fallback"},
            })
        return records

    def _parse_detail_json(self, data, record, detail_url):
        post_id = str(data.get("id") or record.get("external_id") or "")
        if not post_id:
            raise RuntimeError("detail has no post id")

        content_html = self._rendered(data.get("content"))
        excerpt_html = self._rendered(data.get("excerpt"))
        abstract = self._extract_fragment_text(content_html, context=f"content {post_id}")
        abstract_source = "content.rendered"
        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            excerpt_text = self._extract_fragment_text(
                excerpt_html, context=f"excerpt {post_id}"
            )
            if len(excerpt_text) > len(abstract):
                abstract = excerpt_text
                abstract_source = "excerpt.rendered"
        if len(abstract) < self.MIN_ABSTRACT_CHARS and record.get("list_excerpt"):
            abstract = record["list_excerpt"]
            abstract_source = "list excerpt"

        title = (
            self._clean_title(self._rendered(data.get("title")))
            or record.get("title")
            or ""
        )
        if not title:
            raise RuntimeError("detail has no title")

        canonical_url = data.get("link") or record.get("url") or ""
        if not canonical_url:
            raise RuntimeError("detail has no URL")
        canonical_url = urljoin(self.base_url, canonical_url)

        category_names, tag_names = self._parse_terms(data)
        category = ", ".join(category_names) if category_names else "Ανακοινώσεις Τύπου"
        keywords = self._dedupe(tag_names + category_names)

        links = self._extract_links(content_html, canonical_url)
        pdf_links = [lnk for lnk in links if self._looks_like_pdf(lnk["url"])]
        attachment_links = [
            lnk for lnk in links
            if self._looks_like_attachment(lnk["url"]) and not self._looks_like_pdf(lnk["url"])
        ]
        image_urls = self._extract_image_urls(content_html, canonical_url)
        featured_media_url = self._featured_media_url(data)
        if featured_media_url:
            image_urls = self._dedupe([featured_media_url] + image_urls)

        published_date = self._parse_date(data.get("date")) or record.get("published_date", "")

        metadata = {
            "source": "WordPress REST API",
            "starting_url": self.START_URL,
            "list_api_url": record.get("list_api_url"),
            "detail_api_url": detail_url,
            "wp_post_id": post_id,
            "slug": data.get("slug") or record.get("raw_list", {}).get("slug"),
            "date": data.get("date"),
            "date_gmt": data.get("date_gmt"),
            "modified": data.get("modified"),
            "modified_gmt": data.get("modified_gmt"),
            "category_ids": data.get("categories") or [],
            "tag_ids": data.get("tags") or [],
            "categories": category_names,
            "tags": tag_names,
            "abstract_source": abstract_source,
            "featured_media_url": featured_media_url,
            "image_urls": image_urls,
            "pdf_links": pdf_links,
            "attachments": attachment_links,
            "posted_date": data.get("date"),
        }

        pdf_url = pdf_links[0]["url"] if pdf_links else ""
        original_filename = ""
        if pdf_url:
            original_filename = urlparse(pdf_url).path.rsplit("/", 1)[-1] or ""

        return {
            "external_id": post_id,
            "title": title,
            "authors": [],
            "abstract": abstract,
            "category": category,
            "keywords": keywords,
            "published_date": published_date,
            "listed_date": published_date,
            "url": canonical_url,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "publisher": self.DEPARTMENT,
            "department": self.DEPARTMENT,
            "metadata": metadata,
        }

    def _parse_public_detail_fallback(self, record, referer):
        url = record.get("url") or ""
        if not url:
            raise RuntimeError("cannot fetch public HTML fallback without URL")
        raw = self._curl_get(
            url,
            context=f"item {record.get('external_id', '')} public HTML fallback",
            referer=referer,
        )
        if not raw:
            raise RuntimeError("public HTML fallback fetch failed after retries")
        soup = self._make_soup(raw, context=f"public detail {url}")
        if soup is None:
            raise RuntimeError("public detail HTML could not be parsed")

        article = (
            soup.select_one("article[id^='post-']")
            or soup.select_one("article.post")
            or soup.select_one(".single-post article")
            or soup
        )
        title = (
            self._clean_title(self._node_text(article.select_one("h1, .entry-title")))
            or self._clean_title(self._meta_content(soup, "og:title", "twitter:title"))
            or record.get("title")
            or ""
        )
        if not title:
            raise RuntimeError("public detail HTML has no title")

        content_node = (
            article.select_one(".entry-content")
            or article.select_one(".post-content")
            or article.select_one(".post")
            or article
        )
        abstract = self._extract_fragment_text(str(content_node), context=f"public detail {url}")
        canonical_url = (
            self._link_href(soup, "link[rel='canonical']")
            or self._meta_content(soup, "og:url")
            or url
        )
        canonical_url = urljoin(self.base_url, canonical_url)
        post_id = (
            record.get("external_id")
            or self._external_id_from_html(raw)
            or self._external_id_from_url(canonical_url)
        )
        links = self._extract_links(str(content_node), canonical_url)
        pdf_links = [lnk for lnk in links if self._looks_like_pdf(lnk["url"])]

        metadata = {
            "source": "public WordPress detail HTML fallback",
            "starting_url": self.START_URL,
            "list_api_url": record.get("list_api_url"),
            "wp_post_id": post_id,
            "canonical_url": canonical_url,
            "pdf_links": pdf_links,
            "posted_date": self._meta_content(soup, "article:published_time"),
        }

        published_date = (
            self._parse_date(self._meta_content(soup, "article:published_time"))
            or record.get("published_date", "")
        )
        pdf_url = pdf_links[0]["url"] if pdf_links else ""
        original_filename = ""
        if pdf_url:
            original_filename = urlparse(pdf_url).path.rsplit("/", 1)[-1] or ""

        return {
            "external_id": post_id,
            "title": title,
            "authors": [],
            "abstract": abstract,
            "category": "Ανακοινώσεις Τύπου",
            "keywords": ["Ανακοινώσεις Τύπου"],
            "published_date": published_date,
            "listed_date": published_date,
            "url": canonical_url,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "publisher": self.DEPARTMENT,
            "department": self.DEPARTMENT,
            "metadata": metadata,
        }

    # ------------------------------------------------------------------
    # HTML and text helpers
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

    def _extract_fragment_text(self, html_text, context="HTML fragment"):
        html_text = html_text or ""
        if not html_text:
            return ""
        soup = self._make_soup(html_text, context=context)
        if soup is None:
            return self._clean_multiline(re.sub(r"<[^>]+>", " ", html_text))

        for bad in soup.select("script, style, noscript, iframe, svg, form, nav"):
            bad.decompose()

        root = soup.body or soup
        parts = []
        for node in root.find_all(
            ["p", "li", "h2", "h3", "h4", "blockquote"],
            recursive=True,
        ):
            text = self._node_text(node)
            if text and text not in parts:
                parts.append(text)

        if parts:
            return self._clean_multiline("\n\n".join(parts))
        return self._clean_multiline(root.get_text("\n", strip=True))

    def _extract_links(self, html_text, base_url):
        soup = self._make_soup(html_text or "", context=f"links {base_url}")
        if soup is None:
            return []
        links = []
        seen = set()
        for node in soup.find_all("a", href=True):
            href = urljoin(base_url, node.get("href", "").strip())
            if not href or href.startswith("mailto:") or href.startswith("tel:"):
                continue
            if href in seen:
                continue
            seen.add(href)
            links.append({"label": self._node_text(node), "url": href})
        return links

    def _extract_image_urls(self, html_text, base_url):
        soup = self._make_soup(html_text or "", context=f"images {base_url}")
        if soup is None:
            return []
        urls = []
        for node in soup.find_all("img"):
            src = node.get("src") or node.get("data-src") or ""
            if src:
                urls.append(urljoin(base_url, src.strip()))
        return self._dedupe(urls)

    @staticmethod
    def _rendered(value):
        if isinstance(value, dict):
            return str(value.get("rendered") or "")
        if value is None:
            return ""
        return str(value)

    @classmethod
    def _clean_text(cls, value):
        text = unescape(str(value or ""))
        text = text.replace("\xa0", " ")
        text = text.replace("​", "")
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
    def _clean_title(cls, value):
        title = cls._one_line(value)
        title = re.sub(r"\s+\|\s+.*$", "", title)
        title = re.sub(r"\s+-\s+.*?ypergasias.*$", "", title, flags=re.I)
        return title.strip()

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

    # ------------------------------------------------------------------
    # WordPress field helpers
    # ------------------------------------------------------------------

    def _parse_terms(self, data):
        categories = []
        tags = []
        embedded = data.get("_embedded") or {}
        term_groups = embedded.get("wp:term") or []
        for group in term_groups:
            if not isinstance(group, list):
                continue
            for term in group:
                if not isinstance(term, dict) or term.get("code"):
                    continue
                name = self._one_line(term.get("name"))
                if not name:
                    continue
                taxonomy = term.get("taxonomy")
                if taxonomy == "post_tag":
                    tags.append(name)
                elif taxonomy == "category":
                    categories.append(name)
        if not categories:
            categories.append("Ανακοινώσεις Τύπου")
        return self._dedupe(categories), self._dedupe(tags)

    def _featured_media_url(self, data):
        embedded = data.get("_embedded") or {}
        for media in embedded.get("wp:featuredmedia") or []:
            if not isinstance(media, dict) or media.get("code"):
                continue
            source_url = media.get("source_url")
            if source_url:
                return str(source_url).strip()
        return ""

    # ------------------------------------------------------------------
    # Small utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_date(value):
        text = str(value or "").strip()
        if not text:
            return ""
        # NOTE (2026-07-11 fix): trailing \b fails to match when the ISO
        # date is followed by 'T' (WP REST's "YYYY-MM-DDTHH:MM:SS" format)
        # because digit->letter is not a word boundary. Use a
        # not-followed-by-digit lookahead instead.
        iso = re.search(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})(?!\d)", text)
        if iso:
            return iso.group(0)
        return ""

    @staticmethod
    def _external_id_from_html(raw_html):
        match = re.search(r'<article[^>]+id=["\']post-(\d+)["\']', raw_html or "")
        if match:
            return match.group(1)
        return ""

    @staticmethod
    def _external_id_from_url(url):
        path = urlparse(url or "").path.strip("/")
        return path or ""

    @staticmethod
    def _looks_like_pdf(url):
        path = urlparse(url or "").path.lower()
        return path.endswith(".pdf")

    @staticmethod
    def _looks_like_attachment(url):
        path = urlparse(url or "").path.lower()
        return bool(re.search(
            r"\.(pdf|doc|docx|xls|xlsx|ppt|pptx|zip|rar|7z|csv|jpg|jpeg|png)$",
            path,
        ))

    def _is_internal_link(self, url):
        parsed = urlparse(url or "")
        return parsed.netloc in ("", urlparse(self.base_url).netloc)

    @staticmethod
    def _dedupe(values):
        seen = set()
        result = []
        for value in values:
            text = str(value or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
        return result
