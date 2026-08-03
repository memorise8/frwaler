# -*- coding: utf-8 -*-
"""Crawler for Te Kawa Mataaho Public Service Commission data pages.

The site renders data pages as normal Silverstripe HTML detail pages. The
discoverable full-depth list endpoint is the paginated Silverstripe sitemap
under ``/sitemap.xml/sitemap/SilverStripe-CMS-Model-SiteTree/{page}``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Dict, Iterable, List, Optional
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class PublicServiceGovtNzDataCrawler(BaseCrawler):
    site_id = "publicservice-govt-nz-data"
    site_name = "Custom: publicservice-govt-nz-data"
    base_url = "https://www.publicservice.govt.nz"

    START_URL = "https://www.publicservice.govt.nz/data/workforce-data/remunerationpay/workforce-costs"
    SITEMAP_INDEX_URL = "https://www.publicservice.govt.nz/sitemap.xml"
    SITEMAP_PAGE_URL = (
        "https://www.publicservice.govt.nz/sitemap.xml/sitemap/"
        "SilverStripe-CMS-Model-SiteTree/{page}"
    )
    DATA_PREFIX = "https://www.publicservice.govt.nz/data/"
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_RUNTIME_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _RUNTIME_GRACE_SECONDS = 60
    _sitemap_page_urls: Optional[List[str]] = None

    # ------------------------------------------------------------------
    # Fetching / parsing helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, timeout: int = 45) -> Optional[str]:
        """Fetch a URL with curl and retry transient network failures."""
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--max-time",
            str(timeout),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            url,
        ]
        waits = [1, 3, 9]
        last_error = ""
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 5,
                    check=False,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = stderr or f"curl exit {result.returncode}"
            except subprocess.TimeoutExpired as exc:
                last_error = f"timeout after {exc.timeout}s"
            except OSError as exc:
                last_error = str(exc)

            if attempt < 2:
                wait = waits[attempt]
                print(
                    f"[{self.site_id}] fetch failed for {url} "
                    f"(attempt {attempt + 1}/3): {last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] fetch failed after 3 attempts for {url}: {last_error}")
        return None

    def _make_soup(self, raw: str) -> BeautifulSoup:
        """Build BeautifulSoup with the required tolerant parser fallback chain."""
        last_error = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                last_error = exc
                print(f"[{self.site_id}] BeautifulSoup parser {parser} failed: {exc}")
        print(f"[{self.site_id}] all HTML parsers failed: {last_error}")
        return BeautifulSoup("", "html.parser")

    @staticmethod
    def _clean_text(value: str) -> str:
        value = re.sub(r"\s+", " ", value or "")
        return value.replace("\xa0", " ").strip()

    @staticmethod
    def _date_only(value: Optional[str]) -> str:
        if not value:
            return ""
        value = value.strip()
        try:
            if "," in value:
                return parsedate_to_datetime(value).date().isoformat()
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
        except Exception:
            match = re.search(r"\d{4}-\d{2}-\d{2}", value)
            return match.group(0) if match else ""

    def _absolute_url(self, href: str) -> str:
        return urljoin(self.base_url, href or "")

    def _parse_sitemap_page(self, raw: str) -> List[Dict[str, str]]:
        """Parse one Silverstripe sitemap page into candidate list records."""
        records: List[Dict[str, str]] = []
        try:
            root = ElementTree.fromstring(raw.encode("utf-8"))
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            for node in root.findall("sm:url", ns):
                loc_node = node.find("sm:loc", ns)
                if loc_node is None or not loc_node.text:
                    continue
                url = loc_node.text.strip()
                lastmod_node = node.find("sm:lastmod", ns)
                lastmod = lastmod_node.text.strip() if lastmod_node is not None and lastmod_node.text else ""
                if self._is_data_detail_url(url):
                    records.append({"url": url, "lastmod": lastmod, "source": "sitemap"})
            return records
        except Exception as exc:
            print(f"[{self.site_id}] XML sitemap parse failed; trying HTML parser: {exc}")

        soup = self._make_soup(raw)
        for url_node in soup.find_all("url"):
            loc_node = url_node.find("loc")
            if not loc_node:
                continue
            url = self._clean_text(loc_node.get_text(" "))
            lastmod_node = url_node.find("lastmod")
            lastmod = self._clean_text(lastmod_node.get_text(" ")) if lastmod_node else ""
            if self._is_data_detail_url(url):
                records.append({"url": url, "lastmod": lastmod, "source": "sitemap"})
        return records

    def _discover_sitemap_page_urls(self) -> List[str]:
        """Discover the live SilverStripe sitemap list endpoints."""
        if self._sitemap_page_urls is not None:
            return self._sitemap_page_urls

        raw = self._curl_get(self.SITEMAP_INDEX_URL, timeout=30)
        page_urls: List[str] = []
        if raw:
            try:
                root = ElementTree.fromstring(raw.encode("utf-8"))
                ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
                for node in root.findall("sm:sitemap", ns):
                    loc_node = node.find("sm:loc", ns)
                    if loc_node is not None and loc_node.text:
                        loc = loc_node.text.strip()
                        if "/sitemap/SilverStripe-CMS-Model-SiteTree/" in loc:
                            page_urls.append(loc)
            except Exception as exc:
                print(f"[{self.site_id}] sitemap index XML parse failed; trying HTML parser: {exc}")
                soup = self._make_soup(raw)
                for loc_node in soup.find_all("loc"):
                    loc = self._clean_text(loc_node.get_text(" "))
                    if "/sitemap/SilverStripe-CMS-Model-SiteTree/" in loc:
                        page_urls.append(loc)

        if not page_urls:
            page_urls = [self.SITEMAP_PAGE_URL.format(page=1)]

        def sort_key(url: str) -> int:
            match = re.search(r"/(\d+)(?:\?.*)?$", url)
            return int(match.group(1)) if match else 0

        deduped: List[str] = []
        seen = set()
        for url in sorted(page_urls, key=sort_key):
            if url in seen:
                continue
            seen.add(url)
            deduped.append(url)

        self._sitemap_page_urls = deduped
        return deduped

    def _is_data_detail_url(self, url: str) -> bool:
        if not url.startswith(self.DATA_PREFIX):
            return False
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        if not path.startswith("/data/"):
            return False
        if path.endswith("/changes"):
            return False
        if "/assets/" in path:
            return False
        return True

    def _sort_records(self, records: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
        start_path = urlparse(self.START_URL).path.rstrip("/")
        parent_path = start_path.rsplit("/", 1)[0]
        grandparent_path = parent_path.rsplit("/", 1)[0]

        def sort_key(record: Dict[str, str]):
            path = urlparse(record["url"]).path.rstrip("/")
            if path == start_path:
                bucket = 0
            elif path.startswith(parent_path + "/"):
                bucket = 1
            elif path.startswith(grandparent_path + "/"):
                bucket = 2
            else:
                bucket = 3
            return (bucket, path.count("/"), path)

        return sorted(records, key=sort_key)

    def _discover_page_records(self, page: int) -> List[Dict[str, str]]:
        sitemap_pages = self._discover_sitemap_page_urls()
        if page > len(sitemap_pages):
            return []
        list_url = sitemap_pages[page - 1]
        raw = self._curl_get(list_url)
        if not raw:
            return []
        records = self._parse_sitemap_page(raw)
        if page == 1 and not any(r["url"].rstrip("/") == self.START_URL for r in records):
            records.insert(
                0,
                {"url": self.START_URL, "lastmod": "", "source": "starting-url"},
            )
        return self._sort_records(records)

    def _extract_downloads(self, soup: BeautifulSoup) -> List[Dict[str, str]]:
        downloads: List[Dict[str, str]] = []
        main = soup.find("main") or soup
        for link in main.find_all("a", href=True):
            href = link.get("href") or ""
            absolute = self._absolute_url(href)
            path = urlparse(absolute).path.lower()
            if not re.search(r"\.(csv|xlsx?|pdf)$", path):
                continue
            name_node = link.select_one(".link__name-inner")
            meta_node = link.select_one(".link__meta")
            label = self._clean_text(name_node.get_text(" ")) if name_node else self._clean_text(link.get_text(" "))
            meta = self._clean_text(meta_node.get_text(" ")) if meta_node else ""
            downloads.append({"title": label, "url": absolute, "meta": meta})
        return downloads

    def _extract_tableau_views(self, soup: BeautifulSoup) -> List[str]:
        views: List[str] = []
        for node in (soup.find("main") or soup).find_all("tableau-viz"):
            src = node.get("src")
            if src:
                views.append(self._clean_text(src))
        return views

    def _extract_breadcrumbs(self, soup: BeautifulSoup) -> List[str]:
        crumbs: List[str] = []
        container = soup.select_one(".banner__breadcrumbs")
        if not container:
            return crumbs
        for part in container.stripped_strings:
            cleaned = self._clean_text(part)
            if cleaned and cleaned != "/":
                crumbs.append(cleaned)
        return crumbs

    def _extract_title(self, soup: BeautifulSoup) -> str:
        h1_en = soup.select_one("main h1 .title--en")
        if h1_en:
            title = self._clean_text(h1_en.get_text(" "))
            if title:
                return title
        h1 = soup.select_one("main h1")
        if h1:
            title = self._clean_text(h1.get_text(" "))
            if title:
                return title
        og_title = soup.find("meta", attrs={"property": "og:title"})
        if og_title and og_title.get("content"):
            return self._clean_text(og_title["content"].split("|")[0])
        title_node = soup.find("title")
        if title_node:
            return self._clean_text(title_node.get_text(" ").split(" - ")[0])
        return ""

    def _extract_abstract(self, soup: BeautifulSoup) -> str:
        parts: List[str] = []
        intro = soup.select_one("main .banner__intro")
        if intro:
            intro_text = self._clean_text(intro.get_text(" "))
            if intro_text:
                parts.append(intro_text)

        main = soup.find("main") or soup
        for node in main.select(".content-element__content"):
            for unwanted in node.select("script, style, noscript"):
                unwanted.decompose()
            for child in node.find_all(["h2", "h3", "p", "li"], recursive=True):
                text = self._clean_text(child.get_text(" "))
                if not text:
                    continue
                if re.fullmatch(r"[\w .,'’()/-]+\(?(CSV|PDF|XLSX?),? [^)]+\)?", text, re.I):
                    continue
                parts.append(text)

        if len(" ".join(parts)) < 100:
            for tile in main.select(".tile__inner"):
                text = self._clean_text(tile.get_text(" "))
                if text:
                    parts.append(text)

        if len(" ".join(parts)) < 100:
            desc = soup.find("meta", attrs={"name": "description"})
            if desc and desc.get("content"):
                parts.insert(0, self._clean_text(desc["content"]))

        deduped: List[str] = []
        seen = set()
        for part in parts:
            key = part.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(part)
        return self._clean_text(" ".join(deduped))

    def _extract_rss_date(self, soup: BeautifulSoup) -> str:
        link = soup.find("link", attrs={"rel": "alternate", "type": "application/rss+xml"})
        if not link or not link.get("href"):
            return ""
        rss_url = self._absolute_url(link["href"])
        raw = self._curl_get(rss_url, timeout=30)
        if not raw:
            return ""
        try:
            root = ElementTree.fromstring(raw.encode("utf-8"))
            item = root.find("./channel/item")
            if item is None:
                return ""
            pub = item.find("pubDate")
            return self._date_only(pub.text if pub is not None else "")
        except Exception:
            rss_soup = self._make_soup(raw)
            pub = rss_soup.find("pubdate")
            return self._date_only(pub.get_text(" ") if pub else "")

    def _parse_detail(self, raw: str, record: Dict[str, str]) -> Optional[Dict[str, str]]:
        soup = self._make_soup(raw)
        canonical_node = soup.find("link", attrs={"rel": "canonical"})
        canonical = (
            self._absolute_url(canonical_node["href"])
            if canonical_node and canonical_node.get("href")
            else record["url"]
        )
        title = self._extract_title(soup)
        abstract = self._extract_abstract(soup)
        if not title:
            print(f"[{self.site_id}] missing title for {record['url']}; skipping")
            return None
        if len(abstract) < 50:
            print(
                f"[{self.site_id}] short abstract for {canonical} "
                f"({len(abstract)} chars); skipping"
            )
            return None

        downloads = self._extract_downloads(soup)
        pdf_url = ""
        for item in downloads:
            if urlparse(item["url"]).path.lower().endswith(".pdf"):
                pdf_url = item["url"]
                break

        breadcrumbs = self._extract_breadcrumbs(soup)
        category = " > ".join(breadcrumbs) if breadcrumbs else "Data"
        tableau_views = self._extract_tableau_views(soup)
        published_date = self._date_only(record.get("lastmod")) or self._extract_rss_date(soup)
        if not published_date:
            published_date = self._extract_rss_date(soup)

        keyword_parts = ["data"]
        keyword_parts.extend([c.lower() for c in breadcrumbs if c.lower() not in {"data"}])
        keyword_parts.extend([d["title"].lower() for d in downloads if d.get("title")])
        keywords = []
        for part in keyword_parts:
            part = self._clean_text(part)
            if part and part not in keywords:
                keywords.append(part)

        metadata = {
            "canonical_url": canonical,
            "source": record.get("source", "sitemap"),
            "sitemap_lastmod": record.get("lastmod", ""),
            "breadcrumbs": breadcrumbs,
            "downloads": downloads,
            "tableau_views": tableau_views,
        }

        external_id = hashlib.sha1(canonical.encode("utf-8")).hexdigest()
        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, canonical)),
            "site_id": self.site_id,
            "external_id": external_id,
            "title": title,
            "authors": json.dumps(["Te Kawa Mataaho Public Service Commission"], ensure_ascii=False),
            "abstract": abstract,
            "category": category,
            "keywords": json.dumps(keywords, ensure_ascii=False),
            "published_date": published_date,
            "url": canonical,
            "pdf_url": pdf_url,
            "doi": "",
            "department": "Te Kawa Mataaho Public Service Commission",
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls = set()
        limit_or_inf = limit if limit is not None else "inf"
        started = time.monotonic()

        while page <= self._MAX_PAGES:
            if limit is not None and saved >= limit:
                break
            elapsed = time.monotonic() - started
            if elapsed >= self._MAX_RUNTIME_SECONDS - self._RUNTIME_GRACE_SECONDS:
                print(f"[{self.site_id}] approaching 25 minute budget; exiting cleanly")
                break
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            records = self._discover_page_records(page)
            new_records: List[Dict[str, str]] = []
            for record in records:
                url = record.get("url", "").rstrip("/")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                record["url"] = url
                new_records.append(record)

            if not new_records:
                print(f"[{self.site_id}] page {page}: 0 new records; stopping")
                break

            for idx, record in enumerate(new_records, start=1):
                if limit is not None and saved >= limit:
                    break
                elapsed = time.monotonic() - started
                if elapsed >= self._MAX_RUNTIME_SECONDS - self._RUNTIME_GRACE_SECONDS:
                    print(f"[{self.site_id}] approaching 25 minute budget; exiting cleanly")
                    return saved

                try:
                    time.sleep(self._delay)
                    raw = self._curl_get(record["url"])
                    if not raw:
                        print(f"[{self.site_id}] item {idx} failed: empty response")
                        continue
                    paper = self._parse_detail(raw, record)
                    if not paper:
                        continue
                    self._save_paper(paper)
                    saved += 1
                except Exception as exc:
                    print(f"[{self.site_id}] item {idx} failed: {exc}")
                    continue

            page += 1
            if page > len(self._discover_sitemap_page_urls()):
                print(f"[{self.site_id}] page {page - 1}: next page link absent; stopping")
                break

        if page > self._MAX_PAGES:
            print(f"[{self.site_id}] reached safety cap of {self._MAX_PAGES} pages")

        return saved
