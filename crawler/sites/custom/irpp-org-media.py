# -*- coding: utf-8 -*-
"""Crawler for the IRPP media centre."""

import json
import re
import subprocess
import time
from datetime import datetime
from html import unescape
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class IRPPOrgMediaCrawler(BaseCrawler):
    site_id = "irpp-org-media"
    site_name = "Custom: irpp-org-media"
    base_url = "https://irpp.org"

    start_url = "https://irpp.org/media/"
    _CURL_TIMEOUT = 30
    _RETRIES = 3
    _BACKOFFS = (1, 3, 9)
    _META_MARKER = "__IRPP_CURL_META__:"

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url):
        """Fetch a URL with curl and return (text, effective_url)."""
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--max-time",
            str(self._CURL_TIMEOUT),
            "-A",
            self.USER_AGENT,
            "-H",
            "Accept: text/html,application/xhtml+xml,application/json,*/*;q=0.8",
            "-H",
            "Accept-Language: en-US,en;q=0.9",
            "-w",
            "\n" + self._META_MARKER + "%{http_code}\t%{url_effective}",
            url,
        ]

        for attempt in range(self._RETRIES):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self._CURL_TIMEOUT + 10,
                )
                raw = result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace").strip()

                if result.returncode != 0:
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")

                body, http_code, effective_url = self._split_curl_output(raw, url)
                if http_code and int(http_code) >= 400:
                    raise RuntimeError(f"HTTP {http_code} for {effective_url}")
                if not body.strip():
                    raise RuntimeError(f"empty response for {effective_url}")
                return body, effective_url
            except Exception as exc:
                if attempt < self._RETRIES - 1:
                    wait = self._BACKOFFS[attempt]
                    print(
                        f"[{self.site_id}] fetch failed "
                        f"({attempt + 1}/{self._RETRIES}) for {url}: {exc}; "
                        f"retrying in {wait}s"
                    )
                    time.sleep(wait)
                else:
                    print(
                        f"[{self.site_id}] fetch failed after "
                        f"{self._RETRIES} attempts for {url}: {exc}"
                    )
        return None, url

    def _split_curl_output(self, raw, fallback_url):
        marker_pos = raw.rfind("\n" + self._META_MARKER)
        if marker_pos == -1:
            return raw, "", fallback_url
        body = raw[:marker_pos]
        meta = raw[marker_pos + 1 + len(self._META_MARKER):].strip()
        if "\t" not in meta:
            return body, "", fallback_url
        http_code, effective_url = meta.split("\t", 1)
        return body, http_code.strip(), effective_url.strip() or fallback_url

    def _fetch_json(self, url):
        raw, effective_url = self._curl_get(url)
        if not raw:
            return None, effective_url
        try:
            return json.loads(raw), effective_url
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] invalid JSON at {effective_url}: {exc}")
            return None, effective_url

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _soup(raw):
        last_error = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                last_error = exc
                continue
        print(f"[irpp-org-media] BeautifulSoup failed: {last_error}")
        return None

    @classmethod
    def _html_to_text(cls, raw_html):
        raw_html = re.sub(r"<!--.*?-->", " ", raw_html or "", flags=re.S)
        raw_html = re.sub(r"<!-.*?-->", " ", raw_html, flags=re.S)
        soup = cls._soup(raw_html)
        if soup is None:
            return cls._clean_text(re.sub(r"<[^>]+>", " ", raw_html))
        for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
            tag.decompose()
        return cls._clean_text(soup.get_text(" ", strip=True))

    @staticmethod
    def _clean_text(value):
        text = unescape(str(value or ""))
        text = text.replace("\ufeff", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @classmethod
    def _node_text(cls, node):
        if node is None:
            return ""
        return cls._clean_text(node.get_text(" ", strip=True))

    @staticmethod
    def _dedupe(values):
        seen = set()
        result = []
        for value in values:
            clean = re.sub(r"\s+", " ", str(value or "")).strip()
            if clean and clean.lower() not in seen:
                result.append(clean)
                seen.add(clean.lower())
        return result

    @staticmethod
    def _json_array(values):
        return json.dumps(values or [], ensure_ascii=False)

    @staticmethod
    def _json_object(value):
        return json.dumps(value or {}, ensure_ascii=False)

    @staticmethod
    def _with_embed(url):
        parsed = urlparse(url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.setdefault("_embed", "1")
        return urlunparse(parsed._replace(query=urlencode(query)))

    @classmethod
    def _parse_date(cls, value):
        text = cls._clean_text(value)
        if not text:
            return ""
        if "T" in text and len(text) >= 10:
            return text[:10]
        text = text.replace("\xa0", " ")
        text = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", text, flags=re.I)
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        match = re.search(r"([A-Z][a-z]+)\s+(\d{1,2}),\s*(\d{4})", text)
        if match:
            return cls._parse_date(match.group(0))
        match = re.search(r"\d{4}-\d{2}-\d{2}", text)
        if match:
            return match.group(0)
        return ""

    @staticmethod
    def _media_type_from_url(url):
        path = urlparse(url or "").path
        for media_type in ("news-release", "op-ed", "podcast", "video"):
            if f"/{media_type}/" in path:
                return media_type
        if "policyoptions.irpp.org" in urlparse(url or "").netloc:
            return "op-ed"
        return "media"

    def _parse_list_items(self, soup):
        items = []
        seen_urls = set()
        article_nodes = soup.select(
            ".load-more__container .event__list article, "
            ".load-more__container .event__grid article"
        )

        for article in article_nodes:
            link = article.select_one("a.article__link[href]")
            title_node = article.select_one(".article__title")
            if link is None or title_node is None:
                continue

            url = urljoin(self.base_url, link.get("href", ""))
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            media_type = self._media_type_from_url(url)
            icon = article.select_one(".icon__oval")
            if icon is not None:
                for class_name in icon.get("class", []):
                    if class_name.startswith("icon--"):
                        media_type = class_name.replace("icon--", "", 1)
                        break

            items.append({
                "url": url,
                "title": self._node_text(title_node),
                "date": self._node_text(article.select_one(".date-subtitle")),
                "excerpt": self._node_text(article.select_one(".article__subtext")),
                "media_type": media_type,
            })
        return items

    def _next_list_url(self, soup):
        link = soup.select_one("a.load-more-link[href]")
        if link is None:
            return ""
        return urljoin(self.base_url, link.get("href", ""))

    @staticmethod
    def _meta_content(soup, *names):
        if soup is None:
            return ""
        for name in names:
            node = soup.find("meta", attrs={"property": name})
            if node is None:
                node = soup.find("meta", attrs={"name": name})
            if node is not None and node.get("content"):
                return str(node.get("content", "")).strip()
        return ""

    @staticmethod
    def _canonical_url(soup):
        if soup is None:
            return ""
        node = soup.find("link", attrs={"rel": "canonical"})
        if node is not None and node.get("href"):
            return str(node.get("href")).strip()
        return ""

    @staticmethod
    def _detail_json_url(soup):
        if soup is None:
            return ""
        for node in soup.find_all("link"):
            href = str(node.get("href") or "")
            title = str(node.get("title") or "").lower()
            type_attr = str(node.get("type") or "").lower()
            if "/wp-json/" not in href or "oembed" in href:
                continue
            if title == "json" or "application/json" in type_attr:
                return unescape(href)
        return ""

    def _extract_authors(self, soup):
        authors = []
        if soup is None:
            return authors

        for selector in (
            ".Banner__postTitle .AuthorLinks a",
            ".AuthorLinks a",
            "a[rel='author']",
            ".author a",
        ):
            for node in soup.select(selector):
                authors.append(self._node_text(node))
            if authors:
                return self._dedupe(authors)

        detail_node = soup.select_one(".media__details p.details-subtitle")
        if detail_node is None:
            detail_node = soup.select_one("p.details-subtitle")
        detail_text = self._node_text(detail_node)
        if "|" in detail_text:
            before_date = detail_text.split("|", 1)[0].strip()
            before_date = re.sub(r"^with\s+", "", before_date, flags=re.I)
            if before_date and not re.search(r"\b\d{4}\b", before_date):
                authors.extend(re.split(r",| and ", before_date))

        meta_author = self._meta_content(soup, "author")
        if meta_author:
            authors.append(meta_author)

        return self._dedupe(authors)

    def _extract_terms(self, data):
        terms = []
        embedded = data.get("_embedded") if isinstance(data, dict) else None
        if not isinstance(embedded, dict):
            return terms
        for group in embedded.get("wp:term", []) or []:
            for term in group or []:
                if not isinstance(term, dict):
                    continue
                name = self._clean_text(term.get("name"))
                if name:
                    terms.append({
                        "taxonomy": term.get("taxonomy") or "",
                        "name": name,
                        "slug": term.get("slug") or "",
                    })
        return terms

    def _extract_featured_image(self, data):
        embedded = data.get("_embedded") if isinstance(data, dict) else None
        if not isinstance(embedded, dict):
            return ""
        media = embedded.get("wp:featuredmedia") or []
        if media and isinstance(media[0], dict):
            return media[0].get("source_url") or ""
        return ""

    def _extract_pdf_url(self, soup, content_html):
        urls = []
        for source_soup in (soup, self._soup(content_html or "")):
            if source_soup is None:
                continue
            for link in source_soup.find_all("a", href=True):
                href = str(link.get("href") or "")
                if ".pdf" in href.lower():
                    urls.append(urljoin(self.base_url, href))
        return self._dedupe(urls)[0] if urls else ""

    def _fallback_content_from_html(self, soup):
        if soup is None:
            return ""
        for selector in (
            ".media-single__content",
            ".ArticleContent",
            "#jsArticle",
            ".entry-content",
            "article",
            "main",
        ):
            node = soup.select_one(selector)
            text = self._node_text(node)
            if len(text) >= 50:
                return text
        return ""

    def _parse_detail(self, list_item, detail_html, effective_url):
        soup = self._soup(detail_html)
        if soup is None:
            raise RuntimeError("detail HTML could not be parsed")

        json_url = self._detail_json_url(soup)
        data = None
        json_effective_url = ""
        if json_url:
            data, json_effective_url = self._fetch_json(self._with_embed(json_url))

        title = list_item.get("title") or ""
        published_date = self._parse_date(list_item.get("date"))
        content_html = ""
        excerpt_html = ""
        item_url = effective_url
        external_id = ""
        wordpress_type = ""
        wordpress_id = ""
        modified = ""
        terms = []
        featured_image = ""

        if isinstance(data, dict):
            wordpress_type = str(data.get("type") or "")
            wordpress_id = str(data.get("id") or "")
            if wordpress_type and wordpress_id:
                external_id = f"{wordpress_type}:{wordpress_id}"
            title = self._html_to_text((data.get("title") or {}).get("rendered")) or title
            published_date = self._parse_date(data.get("date")) or published_date
            item_url = data.get("link") or item_url
            content_html = (data.get("content") or {}).get("rendered") or ""
            excerpt_html = (data.get("excerpt") or {}).get("rendered") or ""
            modified = data.get("modified") or ""
            terms = self._extract_terms(data)
            featured_image = self._extract_featured_image(data)

        if not external_id:
            canonical = self._canonical_url(soup)
            external_id = canonical or effective_url or list_item.get("url")

        authors = self._extract_authors(soup)
        content_text = self._html_to_text(content_html)
        excerpt_text = self._html_to_text(excerpt_html)
        html_content_text = self._fallback_content_from_html(soup)
        meta_description = self._meta_content(soup, "og:description", "description")
        og_title = self._meta_content(soup, "og:title")
        og_image = self._meta_content(soup, "og:image")
        canonical = self._canonical_url(soup)

        abstract_candidates = [
            content_text,
            html_content_text,
            excerpt_text,
            meta_description,
            list_item.get("excerpt") or "",
        ]
        abstract = ""
        for candidate in abstract_candidates:
            candidate = self._clean_text(candidate)
            if len(candidate) > len(abstract):
                abstract = candidate

        media_type = list_item.get("media_type") or self._media_type_from_url(item_url)
        category = media_type
        keyword_names = [
            term["name"]
            for term in terms
            if term.get("name") and term.get("name").lower() != media_type.lower()
        ]

        if not title:
            title = self._clean_text(og_title.replace(" - IRPP", ""))
        if not published_date:
            published_date = self._parse_date(
                self._meta_content(soup, "article:published_time", "article:modified_time")
            )

        metadata = {
            "mediaType": media_type,
            "listUrl": list_item.get("url") or "",
            "finalUrl": effective_url,
            "canonicalUrl": canonical,
            "jsonUrl": json_effective_url or json_url,
            "wordpressType": wordpress_type,
            "wordpressId": wordpress_id,
            "modified": modified,
            "terms": terms,
            "listExcerpt": list_item.get("excerpt") or "",
            "metaDescription": meta_description,
            "featuredImage": featured_image or og_image,
            "sourceHost": urlparse(item_url or effective_url).netloc,
        }

        department = "Institute for Research on Public Policy"
        if "policyoptions.irpp.org" in urlparse(item_url or effective_url).netloc:
            department = "Policy Options"

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": external_id,
            "title": title,
            "authors": self._json_array(authors),
            "abstract": abstract,
            "category": category,
            "keywords": self._json_array(self._dedupe(keyword_names)),
            "published_date": published_date,
            "url": item_url or canonical or effective_url or list_item.get("url"),
            "pdf_url": self._extract_pdf_url(soup, content_html),
            "doi": "",
            "department": department,
            "metadata": self._json_object(metadata),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page_no = 1
        list_url = self.start_url
        seen_pages = set()
        item_no = 0

        while list_url:
            if limit is not None and saved >= limit:
                break
            if list_url in seen_pages:
                print(f"[{self.site_id}] repeated list URL, stopping: {list_url}")
                break
            seen_pages.add(list_url)

            raw, effective_list_url = self._curl_get(list_url)
            if not raw:
                print(f"[{self.site_id}] list page {page_no} failed; stopping")
                break

            soup = self._soup(raw)
            if soup is None:
                print(f"[{self.site_id}] list page {page_no} parse failed; stopping")
                break

            items = self._parse_list_items(soup)
            if not items:
                print(f"[{self.site_id}] no list items on page {page_no}; stopping")
                break

            print(f"[{self.site_id}] page {page_no}: {len(items)} media items")

            for list_item in items:
                if limit is not None and saved >= limit:
                    break
                item_no += 1
                try:
                    time.sleep(self._delay)
                    detail_html, detail_effective_url = self._curl_get(list_item["url"])
                    if not detail_html:
                        raise RuntimeError("detail fetch failed")

                    paper = self._parse_detail(
                        list_item,
                        detail_html,
                        detail_effective_url,
                    )

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {item_no} skipped: "
                            f"abstract too short ({len(abstract)} chars) "
                            f"for {paper.get('url') or list_item['url']}"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(
                        f"[{self.site_id}] saved {counter}: "
                        f"{paper.get('title', '')[:80]}"
                    )
                except Exception as exc:
                    print(f"[irpp-org-media] item {item_no} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break
            next_url = self._next_list_url(soup)
            if not next_url or next_url == effective_list_url:
                break
            list_url = next_url
            page_no += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
