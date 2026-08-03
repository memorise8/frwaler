# -*- coding: utf-8 -*-
"""Crawler for IISD press releases.

Target:
    https://www.iisd.org/press?article_subtype[10]=10&sort_by=unified_date

The site is Drupal 10. The reliable list endpoint is the rendered Views page:

    /press?article_subtype%5B10%5D=10&sort_by=unified_date&page=N

It also advertises a Views AJAX endpoint in drupalSettings, but the rendered
HTML endpoint carries the same records and has stable pager links. Individual
detail pages are attempted for enrichment, then the crawler falls back to the
list record if Cloudflare serves an interstitial.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

from crawler.base_crawler import BaseCrawler


_START_URL = "https://www.iisd.org/press?article_subtype%5B10%5D=10&sort_by=unified_date"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_BACKOFF = (1, 3, 9)
_MIN_ABSTRACT_CHARS = 50


def _make_soup(raw: str | None):
    """Parse malformed HTML without letting parser failures abort a crawl."""
    if not raw:
        return None
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except ImportError:
        return None

    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _clean_text(value: str | None) -> str:
    text = unescape(value or "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(raw: str | None) -> str | None:
    text = _clean_text(raw)
    if not text:
        return None

    iso_match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if iso_match:
        return f"{iso_match.group(1)}-{iso_match.group(2)}-{iso_match.group(3)}"

    text = re.sub(r"^(Published|Updated|Date)\s*:?\s*", "", text, flags=re.I)
    text = re.sub(r"\s+at\s+\d{1,2}:\d{2}.*$", "", text, flags=re.I)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _slug_from_url(url: str | None) -> str | None:
    path = urlparse(url or "").path.rstrip("/")
    if not path:
        return None
    slug = unquote(path.rsplit("/", 1)[-1])
    return slug or None


def _post_number_from_url(url: str | None) -> str | None:
    path = unquote(urlparse(url or "").path)
    match = re.search(r"(?:node|nid|article)[/-](\d{3,})", path)
    if match:
        return match.group(1)
    match = re.search(r"\b(\d{5,})\b", path)
    if match:
        return match.group(1)
    return _slug_from_url(url)


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    tail = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
    if "." in tail and len(tail) <= 200:
        return tail
    return None


def _is_cloudflare_challenge(raw: str | None) -> bool:
    if not raw:
        return False
    head = raw[:5000].lower()
    return (
        "just a moment" in head
        and ("cf_chl" in head or "enable javascript and cookies" in head)
    )


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
        return "; ".join(names) if names else None
    return None


def _curl_get(url: str, *, referer: str | None = None, retries: int = 3) -> str | None:
    """Fetch a URL via curl with TLS max 1.3 and exponential backoff."""
    headers = [
        "-H", f"User-Agent: {BaseCrawler.USER_AGENT}",
        "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
        "-H", "Accept-Language: en-US,en;q=0.9,ko;q=0.7",
    ]
    if referer:
        headers.extend(["-H", f"Referer: {referer}"])

    cmd = [
        "curl",
        "--tls-max", "1.3",
        "-skL",
        "--compressed",
        "--max-time", "45",
        *headers,
        url,
    ]

    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=55)
            if result.returncode == 0 and result.stdout:
                return result.stdout.decode("utf-8", errors="replace")
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            print(
                f"[iisd-org-press] curl failed "
                f"(attempt {attempt + 1}/{retries}) for {url}: "
                f"returncode={result.returncode} {stderr}"
            )
        except Exception as exc:
            print(f"[iisd-org-press] curl error (attempt {attempt + 1}/{retries}) for {url}: {exc}")

        if attempt < retries - 1:
            wait = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
            time.sleep(wait)
    return None


class IisdOrgPressCrawler(BaseCrawler):
    site_id = "iisd-org-press"
    site_name = "Custom: iisd-org-press"
    base_url = "https://www.iisd.org"

    def crawl(self, limit=None):
        if limit is not None and limit <= 0:
            return 0

        saved = 0
        seen_urls: set[str] = set()
        started_at = time.monotonic()
        limit_or_inf = limit if limit is not None else "inf"

        for page in range(_MAX_PAGES):
            if limit is not None and saved >= limit:
                break
            if time.monotonic() - started_at >= _MAX_WALL_SECONDS:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly.")
                break
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page + 1}: saved {saved}/{limit_or_inf}")

            list_url = self._list_url(page)
            raw = _curl_get(list_url, referer=_START_URL)
            if not raw:
                print(f"[{self.site_id}] page {page + 1}: failed to fetch list page; stopping.")
                break

            items = self._parse_list_page(raw, page, list_url)
            if not items:
                print(f"[{self.site_id}] page {page + 1}: no records found; stopping.")
                break

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

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
                    detail_raw = _curl_get(item_url, referer=list_url)
                    if detail_raw is None:
                        print(f"[{self.site_id}] item {label} failed: detail fetch failed after retries")
                        continue
                    detail = self._parse_detail_page(detail_raw, item_url)
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
                    print(f"[{self.site_id}] item {label} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page + 1}: all records were duplicates; stopping.")
                break
            if not self._has_next_page(raw):
                break
            if page == _MAX_PAGES - 1:
                print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached; stopping.")

        print(f"[{self.site_id}] done. saved {saved} records.")
        return saved

    def _list_url(self, page: int) -> str:
        if page <= 0:
            return _START_URL
        return f"{_START_URL}&page={page}"

    def _parse_list_page(self, raw: str, page: int, list_url: str) -> list[dict]:
        soup = _make_soup(raw)
        if soup is None:
            return self._parse_list_page_regex(raw, page, list_url)

        container = soup.select_one(".view-id-news_lsiting.view-display-id-news_listing") or soup
        rows = container.select(".c-listing__items .views-row")
        if not rows:
            rows = container.select("article.c-list-item")

        items = []
        for row in rows:
            article = row.select_one("article.c-list-item") or row
            link = article.select_one("a.c-list-item__heading-link[href]") or article.find("a", href=True)
            if link is None:
                continue

            url = urljoin(self.base_url, link.get("href", ""))
            title = _clean_text(link.get("title") or link.get_text(" ", strip=True))
            excerpt_node = article.select_one(".c-list-item__excerpt")
            abstract = _clean_text(excerpt_node.get_text(" ", strip=True) if excerpt_node else "")
            subtype_node = article.select_one(".c-list-item__meta-subtype")
            date_node = article.select_one(".c-list-item__meta-date")
            subtype = _clean_text(subtype_node.get_text(" ", strip=True) if subtype_node else "")
            listed_date_raw = _clean_text(date_node.get_text(" ", strip=True) if date_node else "")
            listed_date = _parse_date(listed_date_raw)
            external_id = _post_number_from_url(url) or url

            items.append({
                "url": url,
                "title": title,
                "abstract": abstract,
                "category": subtype or "Press release",
                "listed_date_raw": listed_date_raw,
                "listed_date": listed_date,
                "external_id": external_id,
                "post_number": external_id,
                "slug": _slug_from_url(url),
                "list_page": page + 1,
                "list_url": list_url,
            })
        return items

    def _parse_list_page_regex(self, raw: str, page: int, list_url: str) -> list[dict]:
        items = []
        blocks = re.findall(r"<article\b[^>]*c-list-item[^>]*>(.*?)</article>", raw, flags=re.I | re.S)
        for block in blocks:
            href_m = re.search(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', block, flags=re.I | re.S)
            if not href_m:
                continue
            title_attr = re.search(r'\btitle=["\']([^"\']+)["\']', href_m.group(0), flags=re.I | re.S)
            title = _clean_text(title_attr.group(1) if title_attr else re.sub(r"<[^>]+>", " ", href_m.group(2)))
            excerpt_m = re.search(
                r'<div\b[^>]*class=["\'][^"\']*c-list-item__excerpt[^"\']*["\'][^>]*>(.*?)</div>',
                block,
                flags=re.I | re.S,
            )
            date_m = re.search(
                r'<span\b[^>]*class=["\'][^"\']*c-list-item__meta-date[^"\']*["\'][^>]*>(.*?)</span>',
                block,
                flags=re.I | re.S,
            )
            subtype_m = re.search(
                r'<span\b[^>]*class=["\'][^"\']*c-list-item__meta-subtype[^"\']*["\'][^>]*>(.*?)</span>',
                block,
                flags=re.I | re.S,
            )
            url = urljoin(self.base_url, href_m.group(1))
            listed_date_raw = _clean_text(re.sub(r"<[^>]+>", " ", date_m.group(1))) if date_m else ""
            external_id = _post_number_from_url(url) or url
            items.append({
                "url": url,
                "title": title,
                "abstract": _clean_text(re.sub(r"<[^>]+>", " ", excerpt_m.group(1))) if excerpt_m else "",
                "category": _clean_text(re.sub(r"<[^>]+>", " ", subtype_m.group(1))) if subtype_m else "Press release",
                "listed_date_raw": listed_date_raw,
                "listed_date": _parse_date(listed_date_raw),
                "external_id": external_id,
                "post_number": external_id,
                "slug": _slug_from_url(url),
                "list_page": page + 1,
                "list_url": list_url,
            })
        return items

    def _has_next_page(self, raw: str) -> bool:
        soup = _make_soup(raw)
        if soup is not None:
            link = soup.select_one('a[rel="next"][href]')
            if link is None:
                return False
            href = (link.get("href") or "").strip()
            classes = set(link.get("class") or [])
            return bool(href) and "is-inactive" not in classes
        return bool(re.search(r'<a\b[^>]*rel=["\']next["\'][^>]*href=["\'][^"\']+', raw or "", flags=re.I))

    def _parse_detail_page(self, raw: str, url: str) -> dict:
        if _is_cloudflare_challenge(raw):
            return {"detail_fetch_status": "cloudflare_challenge"}

        result = {
            "detail_fetch_status": "ok",
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
            "canonical_url": None,
            "node_id": None,
        }

        soup = _make_soup(raw)
        if soup is None:
            return result

        canonical = soup.select_one('link[rel="canonical"][href]')
        if canonical:
            result["canonical_url"] = urljoin(self.base_url, canonical.get("href"))
            node_match = re.search(r"/node/(\d+)\b", canonical.get("href") or "")
            if node_match:
                result["node_id"] = node_match.group(1)

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

        title_node = soup.select_one("h1")
        if title_node:
            result["title"] = result["title"] or _clean_text(title_node.get_text(" ", strip=True))

        desc_meta = soup.select_one('meta[name="description"][content], meta[property="og:description"][content]')
        if desc_meta:
            result["abstract"] = result["abstract"] or _clean_text(desc_meta.get("content"))

        keyword_meta = soup.select_one('meta[name="keywords"][content]')
        if keyword_meta:
            result["keywords"] = _clean_text(keyword_meta.get("content")) or None

        date_meta = soup.select_one(
            'meta[property="article:published_time"][content], '
            'meta[name="date"][content], time[datetime]'
        )
        if date_meta:
            raw_date = date_meta.get("content") or date_meta.get("datetime") or date_meta.get_text(" ", strip=True)
            result["published_date_raw"] = result["published_date_raw"] or _clean_text(raw_date)
            result["published_date"] = result["published_date"] or _parse_date(raw_date)

        for selector in (".c-article__date", ".c-article-header__date", ".field--name-field-date"):
            node = soup.select_one(selector)
            if node:
                raw_date = _clean_text(node.get_text(" ", strip=True))
                result["published_date_raw"] = result["published_date_raw"] or raw_date
                result["published_date"] = result["published_date"] or _parse_date(raw_date)
                break

        author_nodes = soup.select('[rel="author"], .c-author, .c-article__author, .field--name-field-authors')
        authors = [_clean_text(n.get_text(" ", strip=True)) for n in author_nodes]
        authors = [a for a in authors if a]
        if authors:
            result["authors"] = result["authors"] or "; ".join(dict.fromkeys(authors))

        body_candidates = []
        for selector in (".field--name-body p", ".c-article__body p", "article p"):
            for node in soup.select(selector)[:4]:
                text = _clean_text(node.get_text(" ", strip=True))
                if text:
                    body_candidates.append(text)
            if body_candidates:
                break
        if body_candidates:
            body_text = " ".join(body_candidates)
            if len(body_text) > len(result.get("abstract") or ""):
                result["abstract"] = body_text

        for link in soup.find_all("a", href=True):
            href = urljoin(url, link.get("href"))
            path = urlparse(href).path.lower()
            if path.endswith(".pdf") or ".pdf/" in path or ".pdf?" in href.lower():
                result["pdf_url"] = href
                result["original_filename"] = _filename_from_url(href)
                break

        doi_match = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", raw, flags=re.I)
        if doi_match:
            result["doi"] = doi_match.group(0)

        return result

    def _build_paper(self, item: dict, detail: dict | None) -> dict:
        detail = detail or {}
        detail_url = detail.get("canonical_url") or item.get("url")
        node_id = detail.get("node_id")
        external_id = node_id or item.get("external_id") or _post_number_from_url(detail_url) or detail_url
        post_number = node_id if node_id and str(node_id).isdigit() else (item.get("post_number") or external_id)

        title = detail.get("title") or item.get("title") or "(untitled)"
        abstract = detail.get("abstract") or item.get("abstract") or ""
        listed_date = item.get("listed_date")
        published_date = detail.get("published_date") or listed_date
        pdf_url = detail.get("pdf_url")
        original_filename = detail.get("original_filename") or _filename_from_url(pdf_url)
        publisher = detail.get("publisher") or "International Institute for Sustainable Development"
        category = detail.get("category") or item.get("category") or "Press release"

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
            "slug": item.get("slug") or _slug_from_url(detail_url),
            "post_number": post_number,
            "category_raw": item.get("category"),
            "detail_fetch_status": detail.get("detail_fetch_status"),
            "list_url": item.get("list_url"),
            "list_page": item.get("list_page"),
            "list_title": item.get("title"),
            "list_abstract": item.get("abstract"),
            "view_name": "news_lsiting",
            "view_display_id": "news_listing",
            "article_subtype_tid": "10",
            "sort_by": "unified_date",
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
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }
