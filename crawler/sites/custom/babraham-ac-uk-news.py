# -*- coding: utf-8 -*-
"""Crawler for Babraham Institute news (https://www.babraham.ac.uk/news/category/news)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class BabrahamAcUkNewsCrawler(BaseCrawler):
    site_id = "babraham-ac-uk-news"
    site_name = "Custom: babraham-ac-uk-news"
    base_url = "https://www.babraham.ac.uk"

    _LIST_URL = "https://www.babraham.ac.uk/news/category/news"
    _PAGE_SIZE = 24  # observed items per page

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url, *, timeout=45):
        cmd = [
            "curl",
            "--tls-max", "1.3",
            "-skL",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: en-GB,en;q=0.9",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            url,
        ]
        waits = [1, 3, 9]
        last_error = "unknown"
        for attempt, wait in enumerate(waits, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=timeout + 10, check=False)
            except Exception as exc:
                last_error = str(exc)
            else:
                raw = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and raw.strip():
                    return raw
                last_error = f"exit={result.returncode}"
            if attempt < len(waits):
                print(f"[{self.site_id}] curl retry {attempt}/{len(waits)} for {url}: {last_error}; wait {wait}s")
                time.sleep(wait)
        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_html(self, raw):
        text = raw or ""
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed: {exc}")
        return None

    @staticmethod
    def _clean(value):
        if value is None:
            return ""
        text = unescape(str(value)).replace("\xa0", " ").replace("​", "")
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def _one_line(cls, value):
        return re.sub(r"\s+", " ", cls._clean(value)).strip()

    @staticmethod
    def _parse_iso_date(raw):
        """Extract YYYY-MM-DD from an ISO datetime string or plain date."""
        if not raw:
            return ""
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
        return m.group(0) if m else ""

    @staticmethod
    def _parse_display_date(raw):
        """Parse DD/MM/YYYY display dates."""
        if not raw:
            return ""
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw.strip())
        if m:
            d, mo, y = m.group(1), m.group(2), m.group(3)
            return f"{y}-{mo.zfill(2)}-{d.zfill(2)}"
        return ""

    # ------------------------------------------------------------------
    # List-page parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, raw):
        """Return list of {title, url, listed_date, keywords} dicts."""
        soup = self._parse_html(raw)
        if soup is None:
            return []

        items = []
        for article in soup.find_all("article"):
            # Title + URL
            h3 = article.find("h3")
            if not h3:
                continue
            link = h3.find("a", href=True)
            if not link:
                continue
            url = urljoin(self.base_url, link["href"].strip())
            title = self._one_line(link.get_text(" ", strip=True))
            if not title:
                continue

            # Date from <time datetime="...">
            time_el = article.find("time")
            listed_date = ""
            if time_el:
                listed_date = self._parse_iso_date(time_el.get("datetime", ""))
                if not listed_date:
                    listed_date = self._parse_display_date(time_el.get_text(strip=True))

            # Keywords from the show-for-large div (tag links)
            keywords = []
            kw_div = article.find(class_="show-for-large")
            if kw_div:
                for a in kw_div.find_all("a", href=True):
                    kw = self._one_line(a.get_text(" ", strip=True))
                    if kw:
                        keywords.append(kw)

            items.append({
                "title": title,
                "url": url,
                "listed_date": listed_date,
                "keywords": keywords,
            })
        return items

    # ------------------------------------------------------------------
    # Detail-page parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, raw, item):
        """Parse a detail page and return a paper dict."""
        soup = self._parse_html(raw)
        if soup is None:
            raise ValueError("all HTML parsers failed")

        url = item["url"]

        # Node ID from Drupal settings JSON
        node_id = ""
        settings_el = soup.find("script", attrs={"data-drupal-selector": "drupal-settings-json"})
        if settings_el and settings_el.string:
            m = re.search(r'"currentPath"\s*:\s*"news[\\/]+(\d+)"', settings_el.string)
            if m:
                node_id = m.group(1)

        # Slug from URL path as fallback external_id
        slug = urlparse(url).path.strip("/").rsplit("/", 1)[-1]
        external_id = node_id or slug

        # Title from <h1> or og:title
        og_title = ""
        og_el = soup.find("meta", property="og:title")
        if og_el and og_el.get("content"):
            og_title = self._one_line(og_el["content"])
        h1 = soup.find("h1")
        title = og_title or (self._one_line(h1.get_text(" ", strip=True)) if h1 else "") or item["title"]

        # Abstract: paragraphs from <main>, skip date lines and short noise
        main = soup.find("main") or soup.find(id="main-content") or soup.find(attrs={"role": "main"})
        abstract = ""
        if main:
            for bad in main.find_all(["nav", "header", "footer", "aside", "script", "style", "noscript"]):
                bad.decompose()
            parts = []
            for p in main.find_all("p"):
                text = self._one_line(p.get_text(" ", strip=True))
                if not text:
                    continue
                # Skip pure date lines like "26/01/2026" or "26 January 2026"
                if re.match(r"^\d{1,2}[/ ]\w+[/ ]\d{4}$", text):
                    continue
                if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", text):
                    continue
                parts.append(text)
            abstract = "\n\n".join(parts)

        if not abstract:
            og_desc = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"})
            if og_desc and og_desc.get("content"):
                abstract = self._clean(og_desc["content"])

        abstract = self._clean(abstract)

        # Published date: from <time> in detail or from listed
        published_date = item.get("listed_date", "")
        time_el = soup.find("time")
        if time_el:
            pd = self._parse_iso_date(time_el.get("datetime", ""))
            if not pd:
                pd = self._parse_display_date(time_el.get_text(strip=True))
            if pd:
                published_date = pd

        # PDF URL: look for links ending in .pdf
        pdf_url = ""
        original_filename = None
        for a in soup.find_all("a", href=True):
            href = urljoin(self.base_url, a["href"])
            if re.search(r"\.pdf(?:[?#]|$)", href, re.I):
                pdf_url = href
                original_filename = href.rsplit("/", 1)[-1].split("?")[0] or None
                break

        # DOI
        doi = ""
        m_doi = re.search(r"https?://(?:dx\.)?doi\.org/(10\.[^\s\"'<>]+)", raw or "")
        if m_doi:
            doi = m_doi.group(1).rstrip(".,)")

        # Keywords: merge list-page keywords with any meta keywords
        keywords = list(item.get("keywords") or [])
        meta_kw_el = soup.find("meta", attrs={"name": "keywords"})
        if meta_kw_el and meta_kw_el.get("content"):
            for k in re.split(r"[,;]", meta_kw_el["content"]):
                k = k.strip()
                if k and k not in keywords:
                    keywords.append(k)

        metadata = {
            "node_id": node_id,
            "slug": slug,
            "canonical_url": url,
            "posted_date": item.get("listed_date", ""),
        }

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": node_id or None,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": item.get("listed_date", ""),
            "url": url,
            "pdf_url": pdf_url,
            "doi": doi,
            "keywords": ",".join(keywords),
            "category": "News",
            "department": "Babraham Institute",
            "publisher": "Babraham Institute",
            "authors": "",
            "journal": "",
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 0
        seen_urls: set[str] = set()
        limit_or_inf = limit if limit is not None else "inf"
        start_time = time.time()
        MAX_PAGES = 200
        MAX_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes

        while True:
            # Limit / safety checks
            if limit is not None and saved >= limit:
                break
            if page >= MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {MAX_PAGES} pages reached; stopping")
                break
            if time.time() - start_time > MAX_SECS:
                print(f"[{self.site_id}] 25-minute wall-clock budget reached; stopping cleanly")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            list_url = self._LIST_URL if page == 0 else f"{self._LIST_URL}?page={page}"
            try:
                raw = self._curl(list_url)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] list page {page} fetch error: {exc}; stopping")
                break

            if not raw:
                print(f"[{self.site_id}] empty response on page {page}; stopping")
                break

            items = self._parse_list_page(raw)
            if not items:
                print(f"[{self.site_id}] no items on page {page}; stopping")
                break

            # Deduplicate — if ALL items on this page were already seen, stop
            new_items = [i for i in items if i["url"] not in seen_urls]
            if not new_items:
                print(f"[{self.site_id}] all items on page {page} already seen; stopping")
                break
            for i in new_items:
                seen_urls.add(i["url"])

            for idx, item in enumerate(new_items, start=1):
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > MAX_SECS:
                    print(f"[{self.site_id}] time budget reached mid-page; stopping")
                    break

                item_label = item["url"]
                try:
                    time.sleep(self._delay)
                    detail_raw = self._curl(item["url"])
                    if not detail_raw:
                        print(f"[{self.site_id}] item {item_label} failed: empty response")
                        continue

                    paper = self._parse_detail(detail_raw, item)

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            page += 1

        print(f"[{self.site_id}] crawl complete: saved {saved} items across {page} pages")
        return saved
