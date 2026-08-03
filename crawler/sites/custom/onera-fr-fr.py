# -*- coding: utf-8 -*-
"""Crawler for ONERA French press releases.

Discovered endpoints:
- List:   https://www.onera.fr/fr/presse?page=N
- Detail: https://www.onera.fr/fr/presse/<slug>

Both endpoints are Drupal-rendered HTML. Detail pages expose the native
Drupal node ID through ``rel=shortlink`` and ``drupal-settings-json``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from html import unescape
from typing import Any
from urllib.parse import unquote, urljoin, urlparse, parse_qs

from crawler.base_crawler import BaseCrawler


_BASE_URL = "https://www.onera.fr"
_LIST_URL = "https://www.onera.fr/fr/presse"
_PUBLISHER = "ONERA"
_CATEGORY = "Communiques de presse"
_BACKOFF = (1, 3, 9)
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_CURL_TIMEOUT = 35
_MIN_ABSTRACT_CHARS = 100

_FR_MONTHS = {
    "janvier": 1,
    "fevrier": 2,
    "février": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "aout": 8,
    "août": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "decembre": 12,
    "décembre": 12,
}


def _compact_text(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", unescape(text).replace("\xa0", " ")).strip()


def _make_soup(raw: str):
    """Build BeautifulSoup with html5lib -> lxml -> html.parser fallback."""
    from bs4 import BeautifulSoup

    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _parse_date(raw: str | None) -> str | None:
    """Parse ONERA dates such as '24/04/2025' or 'Publié le 24/04/2025'."""
    text = _compact_text(raw).lower()
    if not text:
        return None

    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if m:
        day, month, year = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return f"{year:04d}-{month:02d}-{day:02d}"

    m = re.search(r"(\d{1,2})\s+([a-zéûôîàèùç]+)\s+(\d{4})", text, re.I)
    if m:
        month = _FR_MONTHS.get(m.group(2).lower())
        if month:
            return f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(1)):02d}"

    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return m.group(0)
    return None


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    tail = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    tail = unquote(tail)
    if tail and "." in tail and len(tail) <= 240:
        return tail
    return None


def _slug_from_url(url: str | None) -> str | None:
    if not url:
        return None
    slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return unquote(slug) if slug else None


def _class_contains(fragment: str):
    def predicate(value):
        if not value:
            return False
        if isinstance(value, (list, tuple)):
            classes = value
        else:
            classes = str(value).split()
        return any(fragment in cls for cls in classes)

    return predicate


class OneraFrFrCrawler(BaseCrawler):
    site_id = "onera-fr-fr"
    site_name = "Custom: onera-fr-fr"
    base_url = _BASE_URL

    START_PAGE = 1
    DETAIL_DELAY = 1.0

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        started_at = time.time()
        page = self.START_PAGE
        scanned = 0
        limit_or_inf = str(limit) if limit is not None else "inf"

        while scanned < _MAX_PAGES:
            if limit is not None and saved >= limit:
                break
            if time.time() - started_at >= _MAX_WALL_SECONDS:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; stopping cleanly")
                break

            scanned += 1
            list_url = f"{_LIST_URL}?page={page}"
            raw = self._fetch(list_url, context=f"list page {page}")
            if not raw:
                print(f"[{self.site_id}] list page {page}: fetch failed; stopping")
                break

            soup = self._soup(raw, context=f"list page {page}")
            if soup is None:
                print(f"[{self.site_id}] list page {page}: parse failed; stopping")
                break

            items = self._parse_list(soup)
            if not items:
                print(f"[{self.site_id}] page {page}: 0 records; stopping")
                break

            new_items = []
            for item in items:
                item_url = item.get("url")
                if not item_url or item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                new_items.append(item)

            if not new_items:
                print(f"[{self.site_id}] page {page}: no new records; stopping")
                break

            for index, item in enumerate(new_items, start=1):
                if limit is not None and saved >= limit:
                    break
                try:
                    if time.time() - started_at >= _MAX_WALL_SECONDS:
                        print(f"[{self.site_id}] approaching 25-minute wall-clock budget; stopping cleanly")
                        return saved
                    if saved or index > 1:
                        time.sleep(getattr(self, "_delay", self.DETAIL_DELAY) or self.DETAIL_DELAY)

                    if self._process_item(item):
                        saved += 1
                        print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {item.get('title', '')[:70]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    label = item.get("url") or item.get("title") or f"page {page} item {index}"
                    print(f"[{self.site_id}] item {label} failed: {exc}")
                    continue

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            next_page = self._next_page(soup)
            if next_page is None:
                print(f"[{self.site_id}] page {page}: no next page; stopping")
                break
            if next_page <= page:
                print(f"[{self.site_id}] page {page}: paginator did not advance; stopping")
                break
            page = next_page
        else:
            print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached")

        return saved

    def _fetch(self, url: str, context: str = "") -> str | None:
        for attempt, backoff in enumerate(_BACKOFF, start=1):
            try:
                result = subprocess.run(
                    [
                        "curl",
                        "--tls-max",
                        "1.3",
                        "-sk",
                        "-L",
                        "--max-time",
                        str(_CURL_TIMEOUT),
                        "-H",
                        f"User-Agent: {self.USER_AGENT}",
                        "-H",
                        "Accept-Language: fr-FR,fr;q=0.9,en;q=0.8",
                        "-H",
                        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
                        url,
                    ],
                    capture_output=True,
                    timeout=_CURL_TIMEOUT + 10,
                )
                body = result.stdout.decode("utf-8", errors="replace")
                if body.strip():
                    return body
                err = result.stderr.decode("utf-8", errors="replace").strip()
                err = err or f"empty response, curl exit {result.returncode}"
            except subprocess.TimeoutExpired:
                err = "curl timeout"
            except Exception as exc:
                err = str(exc)

            if attempt < len(_BACKOFF):
                print(f"[{self.site_id}] {context}: fetch failed ({err}); retry in {backoff}s")
                time.sleep(backoff)
            else:
                print(f"[{self.site_id}] {context}: fetch failed after {attempt} attempts ({err})")
        return None

    def _soup(self, raw: str, context: str):
        try:
            return _make_soup(raw)
        except Exception as exc:
            print(f"[{self.site_id}] {context}: BeautifulSoup failed: {exc}")
            return None

    def _parse_list(self, soup) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for card in soup.find_all("div", class_=_class_contains("home-card")):
            try:
                link = card.find("a", href=True, class_=_class_contains("stretched-link"))
                if not link:
                    continue
                url = urljoin(self.base_url, link.get("href"))
                path = urlparse(url).path
                if not path.startswith("/fr/presse/"):
                    continue

                title_node = card.find("div", class_=_class_contains("title"))
                title = _compact_text(title_node.get_text(" ", strip=True) if title_node else "")
                if not title:
                    image = card.find("img")
                    title = _compact_text((image.get("title") or image.get("alt")) if image else "")

                date_node = card.find("div", class_=_class_contains("meta"))
                listed_date_raw = _compact_text(date_node.get_text(" ", strip=True) if date_node else "")
                listed_date = _parse_date(listed_date_raw)

                chapo_node = card.find("p", class_=_class_contains("chapo"))
                list_abstract = _compact_text(chapo_node.get_text(" ", strip=True) if chapo_node else "")

                image_url = None
                image = card.find("img")
                if image and image.get("src"):
                    image_url = urljoin(self.base_url, image.get("src"))

                items.append(
                    {
                        "url": url,
                        "title": title,
                        "listed_date_raw": listed_date_raw,
                        "listed_date": listed_date,
                        "list_abstract": list_abstract,
                        "image_url": image_url,
                        "slug": _slug_from_url(url),
                    }
                )
            except Exception as exc:
                print(f"[{self.site_id}] list item parse failed: {exc}")
                continue
        return items

    def _next_page(self, soup) -> int | None:
        link = soup.find("a", rel="next")
        if not link or not link.get("href"):
            return None
        href = link.get("href")
        query = parse_qs(urlparse(href).query)
        values = query.get("page")
        if values:
            try:
                return int(values[0])
            except (TypeError, ValueError):
                return None
        m = re.search(r"[?&]page=(\d+)", href)
        if m:
            return int(m.group(1))
        return None

    def _process_item(self, item: dict[str, Any]) -> bool:
        url = item.get("url")
        if not url:
            print(f"[{self.site_id}] item missing url; skipping")
            return False

        raw = self._fetch(url, context=f"detail {item.get('slug') or url}")
        if not raw:
            print(f"[{self.site_id}] item {url} failed: detail fetch returned no body")
            return False

        soup = self._soup(raw, context=f"detail {url}")
        if soup is None:
            print(f"[{self.site_id}] item {url} failed: detail parse returned no soup")
            return False

        detail = self._parse_detail(soup, raw, item)
        title = detail.get("title") or item.get("title") or "(untitled)"
        abstract = detail.get("abstract") or item.get("list_abstract") or detail.get("meta_description") or ""
        abstract = _compact_text(abstract)
        if len(abstract) < _MIN_ABSTRACT_CHARS:
            print(f"[{self.site_id}] abstract too short ({len(abstract)} chars) for {url}; skipping")
            return False

        listed_date = item.get("listed_date")
        published_date = detail.get("published_date") or listed_date
        node_id = detail.get("node_id")
        slug = item.get("slug") or _slug_from_url(url)
        external_id = node_id or slug or url
        post_number = node_id or slug
        pdf_url = detail.get("pdf_url")
        original_filename = detail.get("original_filename") or _filename_from_url(pdf_url)
        canonical_url = detail.get("canonical_url") or url

        metadata = {
            "posted_date": item.get("listed_date_raw"),
            "listed_date_raw": item.get("listed_date_raw"),
            "listed_date": listed_date,
            "published_date_raw": detail.get("published_date_raw"),
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "node_id": node_id,
            "current_path": detail.get("current_path"),
            "shortlink": detail.get("shortlink"),
            "slug": slug,
            "image_url": item.get("image_url"),
            "og_url": detail.get("og_url"),
            "meta_description": detail.get("meta_description"),
            "pdf_filename": original_filename,
            "content_type": "communiques",
            "list_title": item.get("title"),
        }

        paper = {
            "id": f"{self.site_id}:{external_id}",
            "site_id": self.site_id,
            "external_id": str(external_id),
            "post_number": str(post_number) if post_number else None,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": None,
            "publisher": _PUBLISHER,
            "department": None,
            "journal": None,
            "url": canonical_url,
            "pdf_url": pdf_url,
            "keywords": None,
            "category": _CATEGORY,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }
        self._save_paper(paper)
        return True

    def _parse_detail(self, soup, raw: str, item: dict[str, Any]) -> dict[str, Any]:
        title = ""
        h1 = soup.find("h1")
        if h1:
            title = _compact_text(h1.get_text(" ", strip=True))
        if not title:
            title = self._meta_content(soup, "og:title") or self._meta_content(soup, "twitter:title") or ""

        detail_block = self._detail_block(soup)
        date_raw = ""
        if detail_block:
            date_node = detail_block.find("div", class_=_class_contains("date"))
            date_raw = _compact_text(date_node.get_text(" ", strip=True) if date_node else "")
        if not date_raw:
            date_node = soup.find("div", class_=_class_contains("date"))
            date_raw = _compact_text(date_node.get_text(" ", strip=True) if date_node else "")
        published_date = _parse_date(date_raw)

        abstract = ""
        if detail_block:
            wysiwyg = detail_block.find("div", class_=_class_contains("wysiwyg"))
            if wysiwyg:
                abstract = _compact_text(wysiwyg.get_text(" ", strip=True))
        meta_description = (
            self._meta_content(soup, "description", attr="name")
            or self._meta_content(soup, "og:description")
            or self._meta_content(soup, "twitter:description", attr="name")
        )
        if len(abstract) < _MIN_ABSTRACT_CHARS and meta_description:
            abstract = _compact_text(meta_description)

        pdf_url = None
        original_filename = None
        search_root = detail_block if detail_block else soup
        files_block = search_root.find("div", class_=_class_contains("fichiers")) if search_root else None
        pdf_link = None
        for root in (files_block, search_root, soup):
            if not root:
                continue
            for link in root.find_all("a", href=True):
                href = link.get("href")
                if link.get("type") == "application/pdf" or ".pdf" in href.lower():
                    pdf_link = link
                    break
            if pdf_link:
                break
        if pdf_link:
            pdf_url = urljoin(self.base_url, pdf_link.get("href"))
            link_text = _compact_text(pdf_link.get_text(" ", strip=True))
            if link_text.lower().endswith(".pdf"):
                original_filename = link_text
            if not original_filename:
                original_filename = _filename_from_url(pdf_url)

        shortlink = None
        shortlink_el = soup.find("link", rel="shortlink")
        if shortlink_el and shortlink_el.get("href"):
            shortlink = urljoin(self.base_url, shortlink_el.get("href"))

        current_path = None
        node_id = self._node_id_from_shortlink(shortlink)
        settings_tag = soup.find("script", attrs={"data-drupal-selector": "drupal-settings-json"})
        if settings_tag:
            try:
                settings = json.loads(settings_tag.get_text() or "{}")
                current_path = settings.get("path", {}).get("currentPath")
                if not node_id and current_path:
                    m = re.match(r"node/(\d+)$", current_path)
                    if m:
                        node_id = m.group(1)
            except Exception:
                pass
        if not node_id:
            m = re.search(r'"currentPath"\s*:\s*"node/(\d+)"', raw)
            if m:
                node_id = m.group(1)

        og_url = self._meta_content(soup, "og:url")
        canonical_url = og_url or item.get("url")

        return {
            "title": title,
            "abstract": abstract,
            "published_date_raw": date_raw,
            "published_date": published_date,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "node_id": node_id,
            "current_path": current_path,
            "shortlink": shortlink,
            "og_url": og_url,
            "canonical_url": canonical_url,
            "meta_description": meta_description,
        }

    def _detail_block(self, soup):
        for block in soup.find_all("div", class_=_class_contains("bg-white")):
            if block.find("div", class_=_class_contains("wysiwyg")):
                return block
        return None

    def _meta_content(self, soup, name: str, attr: str = "property") -> str | None:
        tag = soup.find("meta", attrs={attr: name})
        if tag and tag.get("content"):
            return _compact_text(tag.get("content"))
        if attr == "property":
            tag = soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                return _compact_text(tag.get("content"))
        return None

    def _node_id_from_shortlink(self, shortlink: str | None) -> str | None:
        if not shortlink:
            return None
        m = re.search(r"/node/(\d+)(?:$|[/?#])", shortlink)
        return m.group(1) if m else None
