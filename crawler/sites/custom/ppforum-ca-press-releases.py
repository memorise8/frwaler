# -*- coding: utf-8 -*-
"""Crawler for Public Policy Forum press releases.

Target:
    https://ppforum.ca/press-releases/

The site is WordPress, but the press-release archive itself is rendered by a
custom page template. The REST page endpoint exists
(/wp-json/wp/v2/pages/106265) but carries an empty content field, so the list
endpoint used here is the rendered HTML archive. Detail URLs are a mix of
Newswire, local PPF pages, and Mailchimp campaign pages.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime
from html import unescape
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


_START_URL = "https://ppforum.ca/press-releases/"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_BACKOFFS = (1, 3, 9)
_MIN_ABSTRACT_CHARS = 50
_META_MARKER = "__PPFORUM_CURL_META__:"


def _make_soup(raw: str | None):
    """Parse malformed HTML without letting parser failures abort a crawl."""
    if not raw:
        return None
    last_error = None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception as exc:
            last_error = exc
            continue
    print(f"[ppforum-ca-press-releases] BeautifulSoup failed: {last_error}")
    return None


def _clean_text(value: str | None) -> str:
    text = unescape(value or "").replace("\xa0", " ").replace("\ufeff", " ")
    return re.sub(r"\s+", " ", text).strip()


def _html_to_text(raw_html: str | None) -> str:
    soup = _make_soup(raw_html or "")
    if soup is None:
        return _clean_text(re.sub(r"<[^>]+>", " ", raw_html or ""))
    for tag in soup(["script", "style", "noscript", "iframe", "svg", "form"]):
        tag.decompose()
    return _clean_text(soup.get_text(" ", strip=True))


def _parse_date(raw: str | None) -> str | None:
    text = _clean_text(raw)
    if not text:
        return None

    if "T" in text and re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:10]

    iso_match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if iso_match:
        return f"{iso_match.group(1)}-{iso_match.group(2)}-{iso_match.group(3)}"

    text = re.sub(r"^(Published|Updated|Date)\s*:?\s*", "", text, flags=re.I)
    text = re.sub(r"\s+at\s+\d{1,2}:\d{2}.*$", "", text, flags=re.I)
    text = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", text, flags=re.I)
    for fmt in (
        "%B %d, %Y",
        "%b %d, %Y",
        "%d %B %Y",
        "%d %b %Y",
        "%B %d %Y",
        "%b %d %Y",
    ):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    match = re.search(r"([A-Z][a-z]+)\s+\d{1,2},\s*\d{4}", text)
    if match:
        return _parse_date(match.group(0))
    return None


def _dedupe_keep_order(values):
    seen = set()
    result = []
    for value in values:
        clean = _clean_text(str(value or ""))
        key = clean.lower()
        if clean and key not in seen:
            seen.add(key)
            result.append(clean)
    return result


def _json_object(value: dict) -> str:
    return json.dumps(value or {}, ensure_ascii=False)


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    tail = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
    if "." in tail and len(tail) <= 200:
        return tail
    return None


def _slug_from_url(url: str | None) -> str | None:
    parsed = urlparse(url or "")
    path = unquote(parsed.path.rstrip("/"))
    if path:
        slug = path.rsplit("/", 1)[-1]
        if slug:
            return slug
    query = parse_qs(parsed.query)
    for key in ("id", "p", "u"):
        if query.get(key) and query[key][0]:
            return query[key][0]
    return None


def _native_id_from_url(url: str | None) -> str | None:
    parsed = urlparse(url or "")
    query = parse_qs(parsed.query)
    if query.get("id") and query["id"][0]:
        return query["id"][0]

    path = unquote(parsed.path)
    match = re.search(r"-(\d{6,})\.html(?:$|[?#])?", path)
    if match:
        return match.group(1)
    match = re.search(r"\b(\d{6,})\b", path)
    if match:
        return match.group(1)
    return _slug_from_url(url)


def _jsonld_objects(value):
    if isinstance(value, list):
        for item in value:
            yield from _jsonld_objects(item)
    elif isinstance(value, dict):
        yield value
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from _jsonld_objects(item)


def _names_from_jsonld(value) -> str | None:
    if not value:
        return None
    if isinstance(value, str):
        return _clean_text(value) or None
    if isinstance(value, dict):
        return _clean_text(value.get("name")) or None
    if isinstance(value, list):
        names = []
        for item in value:
            name = _names_from_jsonld(item)
            if name:
                names.append(name)
        return "; ".join(_dedupe_keep_order(names)) if names else None
    return None


def _meta_content(soup, selectors: tuple[str, ...]) -> str | None:
    if soup is None:
        return None
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            value = node.get("content") or node.get("value") or node.get("datetime")
            if value:
                return _clean_text(value)
    return None


def _curl_get(url: str, *, referer: str | None = None, retries: int = 3) -> tuple[str | None, str]:
    headers = [
        "-H",
        f"User-Agent: {BaseCrawler.USER_AGENT}",
        "-H",
        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
        "-H",
        "Accept-Language: en-US,en;q=0.9,ko;q=0.7",
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
        "45",
        *headers,
        "-w",
        "\n" + _META_MARKER + "%{http_code}\t%{url_effective}",
        url,
    ]

    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=55)
            stdout = result.stdout.decode("utf-8", errors="replace")
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            body, http_code, effective_url = _split_curl_output(stdout, url)

            if result.returncode != 0:
                raise RuntimeError(stderr or f"curl exit {result.returncode}")
            if http_code and int(http_code) >= 400:
                raise RuntimeError(f"HTTP {http_code} for {effective_url}")
            if not body.strip():
                raise RuntimeError(f"empty response for {effective_url}")
            return body, effective_url
        except Exception as exc:
            print(
                f"[ppforum-ca-press-releases] curl failed "
                f"(attempt {attempt + 1}/{retries}) for {url}: {exc}"
            )
            if attempt < retries - 1:
                time.sleep(_BACKOFFS[min(attempt, len(_BACKOFFS) - 1)])
    return None, url


def _split_curl_output(raw: str, fallback_url: str) -> tuple[str, str, str]:
    marker_pos = raw.rfind("\n" + _META_MARKER)
    if marker_pos == -1:
        return raw, "", fallback_url
    body = raw[:marker_pos]
    meta = raw[marker_pos + 1 + len(_META_MARKER):].strip()
    if "\t" not in meta:
        return body, "", fallback_url
    http_code, effective_url = meta.split("\t", 1)
    return body, http_code.strip(), effective_url.strip() or fallback_url


class PPForumCAPressReleasesCrawler(BaseCrawler):
    site_id = "ppforum-ca-press-releases"
    site_name = "Custom: ppforum-ca-press-releases"
    base_url = "https://ppforum.ca"

    def crawl(self, limit=None):
        if limit is not None and limit <= 0:
            return 0

        saved = 0
        seen_urls: set[str] = set()
        started_at = time.monotonic()
        limit_or_inf = limit if limit is not None else "inf"

        for page in range(1, _MAX_PAGES + 1):
            if limit is not None and saved >= limit:
                break
            if time.monotonic() - started_at >= _MAX_WALL_SECONDS - 30:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly.")
                break
            if page == 1 or page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            list_url = self._list_url(page)
            raw, effective_url = _curl_get(list_url, referer=_START_URL)
            if not raw:
                print(f"[{self.site_id}] page {page}: list fetch failed; stopping.")
                break

            items = self._parse_list_page(raw, page, effective_url)
            if not items:
                print(f"[{self.site_id}] page {page}: no records found; stopping.")
                break

            new_on_page = 0
            for item_index, item in enumerate(items, start=1):
                if limit is not None and saved >= limit:
                    break
                if time.monotonic() - started_at >= _MAX_WALL_SECONDS - 30:
                    print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly.")
                    return saved

                item_url = item.get("url")
                if not item_url:
                    continue
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                new_on_page += 1

                label = item.get("external_id") or item_url
                try:
                    time.sleep(getattr(self, "_delay", 1.0))
                    detail_raw, effective_detail_url = _curl_get(item_url, referer=effective_url)
                    if detail_raw is None:
                        print(f"[{self.site_id}] item {item_index} failed: detail fetch failed after retries")
                        continue

                    detail = self._parse_detail_page(detail_raw, effective_detail_url, item)
                    paper = self._build_paper(item, detail)
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < _MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {label}: abstract too short "
                            f"({len(abstract)} chars < {_MIN_ABSTRACT_CHARS}); skipping."
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_index} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: all records were duplicates; stopping.")
                break
            if not self._has_next_page(raw):
                break
            if page == _MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached; stopping.")

        print(f"[{self.site_id}] done. saved {saved} records.")
        return saved

    def _list_url(self, page: int) -> str:
        if page <= 1:
            return _START_URL
        return urljoin(_START_URL, f"page/{page}/")

    def _parse_list_page(self, raw: str, page: int, list_url: str) -> list[dict]:
        soup = _make_soup(raw)
        if soup is None:
            return self._parse_list_page_regex(raw, page, list_url)

        items = []
        main = soup.select_one("main") or soup
        for link in main.find_all("a", href=True):
            title_node = link.find("h2")
            if title_node is None:
                continue

            paragraphs = link.find_all("p")
            if len(paragraphs) < 2:
                continue

            url = urljoin(list_url, link.get("href", ""))
            title = _clean_text(title_node.get_text(" ", strip=True))
            source_raw = _clean_text(paragraphs[0].get_text(" ", strip=True))
            listed_date_raw = _clean_text(paragraphs[-1].get_text(" ", strip=True))
            listed_date = _parse_date(listed_date_raw)
            external_id = _native_id_from_url(url) or url

            if not title or not url:
                continue
            items.append({
                "url": url,
                "title": title,
                "source": source_raw,
                "listed_date_raw": listed_date_raw,
                "listed_date": listed_date,
                "external_id": str(external_id),
                "post_number": str(external_id),
                "slug": _slug_from_url(url),
                "list_page": page,
                "list_url": list_url,
                "list_endpoint": _START_URL,
            })
        return items

    def _parse_list_page_regex(self, raw: str, page: int, list_url: str) -> list[dict]:
        items = []
        blocks = re.findall(r"<a\b[^>]*\bhref=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", raw or "", flags=re.I | re.S)
        for href, block in blocks:
            title_match = re.search(r"<h2\b[^>]*>(.*?)</h2>", block, flags=re.I | re.S)
            if not title_match:
                continue
            paragraphs = re.findall(r"<p\b[^>]*>(.*?)</p>", block, flags=re.I | re.S)
            if len(paragraphs) < 2:
                continue
            url = urljoin(list_url, href)
            title = _html_to_text(title_match.group(1))
            listed_date_raw = _html_to_text(paragraphs[-1])
            external_id = _native_id_from_url(url) or url
            items.append({
                "url": url,
                "title": title,
                "source": _html_to_text(paragraphs[0]),
                "listed_date_raw": listed_date_raw,
                "listed_date": _parse_date(listed_date_raw),
                "external_id": str(external_id),
                "post_number": str(external_id),
                "slug": _slug_from_url(url),
                "list_page": page,
                "list_url": list_url,
                "list_endpoint": _START_URL,
            })
        return items

    def _has_next_page(self, raw: str) -> bool:
        soup = _make_soup(raw)
        if soup is None:
            return bool(re.search(r'rel=[\"\']next[\"\']', raw or "", flags=re.I))
        next_link = soup.select_one('a[rel="next"][href], .pagination a.next[href], nav.pagination a[href]')
        if not next_link:
            return False
        href = (next_link.get("href") or "").strip()
        return bool(href)

    def _parse_detail_page(self, raw: str, url: str, item: dict) -> dict:
        soup = _make_soup(raw)
        result = {
            "detail_fetch_status": "ok",
            "url": url,
            "canonical_url": None,
            "node_id": None,
            "title": None,
            "abstract": None,
            "published_date": None,
            "published_date_raw": None,
            "authors": None,
            "publisher": None,
            "keywords": None,
            "category": None,
            "doi": None,
            "pdf_url": None,
            "original_filename": None,
            "raw_description": None,
            "detail_source": urlparse(url).netloc,
        }
        if soup is None:
            result["abstract"] = _clean_text(item.get("title"))
            result["detail_fetch_status"] = "parse_failed"
            return result

        canonical = soup.select_one('link[rel="canonical"][href]')
        if canonical:
            result["canonical_url"] = urljoin(url, canonical.get("href"))

        article = soup.find("article", id=re.compile(r"^post-\d+$"))
        if article:
            match = re.search(r"post-(\d+)", article.get("id") or "")
            if match:
                result["node_id"] = match.group(1)

        self._parse_jsonld(soup, result)
        self._parse_meta_tags(soup, result)

        title_node = soup.select_one("main h1, article h1, h1")
        if title_node:
            result["title"] = result["title"] or _clean_text(title_node.get_text(" ", strip=True))

        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if "newswire.ca" in host:
            result["abstract"] = self._parse_newswire_body(soup) or result.get("abstract")
            result["publisher"] = result.get("authors") or "Public Policy Forum"
            result["category"] = "Newswire Press Release"
        elif "mailchi.mp" in host or "campaign-archive.com" in host:
            result["abstract"] = self._parse_mailchimp_body(soup) or result.get("abstract")
            result["publisher"] = "Public Policy Forum"
            result["category"] = "Mailchimp Press Release"
        else:
            result["abstract"] = self._parse_ppforum_body(soup) or result.get("abstract")
            result["publisher"] = result.get("publisher") or "Public Policy Forum"
            result["category"] = "Public Policy Forum Press Release"

        for link in soup.find_all("a", href=True):
            href = urljoin(url, link.get("href"))
            if ".pdf" in href.lower() or urlparse(href).path.lower().endswith(".pdf"):
                result["pdf_url"] = href
                result["original_filename"] = _filename_from_url(href)
                break

        doi_match = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", raw, flags=re.I)
        if doi_match:
            result["doi"] = doi_match.group(0)

        return result

    def _parse_jsonld(self, soup, result: dict) -> None:
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                data = json.loads(script.string or script.get_text() or "")
            except (TypeError, ValueError):
                continue
            for obj in _jsonld_objects(data):
                obj_type = obj.get("@type")
                types = obj_type if isinstance(obj_type, list) else [obj_type]
                if not any(t in ("NewsArticle", "Article", "ReportageNewsArticle", "WebPage") for t in types):
                    continue
                result["title"] = result["title"] or _clean_text(obj.get("headline") or obj.get("name"))
                result["abstract"] = result["abstract"] or _clean_text(obj.get("description"))
                result["published_date_raw"] = result["published_date_raw"] or obj.get("datePublished")
                result["published_date"] = result["published_date"] or _parse_date(obj.get("datePublished"))
                result["authors"] = result["authors"] or _names_from_jsonld(obj.get("author"))
                result["publisher"] = result["publisher"] or _names_from_jsonld(obj.get("publisher"))
                keywords = obj.get("keywords")
                if isinstance(keywords, list):
                    result["keywords"] = result["keywords"] or ", ".join(_dedupe_keep_order(keywords))
                elif isinstance(keywords, str):
                    result["keywords"] = result["keywords"] or _clean_text(keywords)

    def _parse_meta_tags(self, soup, result: dict) -> None:
        result["title"] = result["title"] or _meta_content(
            soup,
            ('meta[property="og:title"][content]', 'meta[name="twitter:title"][content]'),
        )
        result["raw_description"] = _meta_content(
            soup,
            (
                'meta[name="description"][content]',
                'meta[property="og:description"][content]',
                'meta[name="twitter:description"][content]',
                'meta[itemprop="description"][content]',
            ),
        )
        result["abstract"] = result["abstract"] or result["raw_description"]

        raw_date = _meta_content(
            soup,
            (
                'meta[property="article:published_time"][content]',
                'meta[name="date"][content]',
                'time[datetime]',
            ),
        )
        if raw_date:
            result["published_date_raw"] = result["published_date_raw"] or raw_date
            result["published_date"] = result["published_date"] or _parse_date(raw_date)

        result["authors"] = result["authors"] or _meta_content(
            soup,
            ('meta[name="author"][content]', 'meta[property="article:author"][content]'),
        )
        result["publisher"] = result["publisher"] or _meta_content(
            soup,
            ('meta[name="Publisher"][content]', 'meta[name="publisher"][content]'),
        )
        result["keywords"] = result["keywords"] or _meta_content(
            soup,
            ('meta[name="keywords"][content]', 'meta[itemprop="keywords"][content]'),
        )

    def _parse_newswire_body(self, soup) -> str | None:
        container = soup.select_one("section.release-body") or soup.select_one(".release-body")
        if not container:
            return None
        texts = []
        for node in container.find_all(["p", "li"]):
            text = _clean_text(node.get_text(" ", strip=True))
            if not text:
                continue
            if text.upper().startswith("SOURCE "):
                break
            if text.lower().startswith("for more information"):
                break
            texts.append(text)
        return " ".join(texts) if texts else None

    def _parse_ppforum_body(self, soup) -> str | None:
        container = soup.select_one("#the-content") or soup.select_one("article .font-serif") or soup.select_one("article")
        if not container:
            return None
        texts = []
        for node in container.find_all(["p", "li", "blockquote"]):
            text = _clean_text(node.get_text(" ", strip=True))
            if not text:
                continue
            lower = text.lower()
            if lower in {"share", "home"}:
                continue
            if lower.startswith("be the first to know"):
                continue
            texts.append(text)
        return " ".join(_dedupe_keep_order(texts)) if texts else None

    def _parse_mailchimp_body(self, soup) -> str | None:
        containers = soup.select(".mcnTextContent")
        if not containers:
            containers = soup.select("body")
        texts = []
        skip_phrases = (
            "view in browser",
            "unsubscribe",
            "update subscription preferences",
            "add us to your address book",
            "copyright",
            "all rights reserved",
        )
        for node in containers:
            text = _clean_text(node.get_text(" ", strip=True))
            if not text:
                continue
            lower = text.lower()
            if any(phrase in lower for phrase in skip_phrases):
                continue
            if len(text) < 20 and lower not in {"media release", "press release"}:
                continue
            texts.append(text)
        return " ".join(_dedupe_keep_order(texts)) if texts else None

    def _build_paper(self, item: dict, detail: dict | None) -> dict:
        detail = detail or {}
        detail_url = detail.get("canonical_url") or detail.get("url") or item.get("url")
        node_id = detail.get("node_id")
        external_id = node_id or item.get("external_id") or _native_id_from_url(detail_url) or detail_url
        post_number = node_id if node_id and str(node_id).isdigit() else (item.get("post_number") or external_id)

        title = detail.get("title") or item.get("title") or "(untitled)"
        title = re.sub(r"\s+-\s+Public Policy Forum$", "", title).strip()
        abstract = detail.get("abstract") or ""
        listed_date = item.get("listed_date")
        published_date = detail.get("published_date") or listed_date
        pdf_url = detail.get("pdf_url")
        original_filename = detail.get("original_filename") or _filename_from_url(pdf_url)
        source = item.get("source") or "Public Policy Forum"
        publisher = detail.get("publisher") or source or "Public Policy Forum"
        category = detail.get("category") or "Press Release"

        metadata = {
            "posted_date": item.get("listed_date_raw"),
            "listed_date": listed_date,
            "listed_date_raw": item.get("listed_date_raw"),
            "published_date_raw": detail.get("published_date_raw"),
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "node_id": node_id,
            "native_id": external_id,
            "post_number": post_number,
            "slug": item.get("slug") or _slug_from_url(detail_url),
            "source": source,
            "source_raw": item.get("source"),
            "category_raw": category,
            "detail_fetch_status": detail.get("detail_fetch_status"),
            "detail_source": detail.get("detail_source"),
            "raw_description": detail.get("raw_description"),
            "list_endpoint": item.get("list_endpoint"),
            "list_url": item.get("list_url"),
            "list_page": item.get("list_page"),
            "list_title": item.get("title"),
            "wp_rest_page_endpoint": "https://ppforum.ca/wp-json/wp/v2/pages/106265",
            "wp_rest_page_content_empty": True,
            "canonical_url": detail.get("canonical_url"),
            "pdf_url_raw": pdf_url,
        }
        metadata = {k: v for k, v in metadata.items() if v not in (None, "", [], {})}

        return {
            "id": f"{self.site_id}-{external_id}",
            "site_id": self.site_id,
            "external_id": str(external_id) if external_id is not None else None,
            "post_number": str(post_number) if post_number is not None else None,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": detail.get("authors"),
            "publisher": publisher,
            "department": None,
            "journal": None,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": detail.get("keywords"),
            "category": category,
            "doi": detail.get("doi"),
            "original_filename": original_filename,
            "metadata": _json_object(metadata),
        }
