# -*- coding: utf-8 -*-
"""Crawler for om-mp.be/nl persberichten (Belgisch Openbaar Ministerie).

Starting URL: https://www.om-mp.be/nl/onze-publicaties?f%5B0%5D=type%3A2

Drupal 10 faceted listing of press releases (type=2, ~4000+ items).
Pagination: ?f%5B0%5D=type%3A2&page=N  (0-based, 10 items/page)
Detail URLs: /nl/artikel/<slug>  or  /nl/blog-post/<slug>
Abstract:    full body from field--name-field-body on each detail page
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Absolute import — no package context when loaded via spec_from_file_location
_CRAWLER_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_CRAWLER_ROOT) not in sys.path:
    sys.path.insert(0, str(_CRAWLER_ROOT))

from crawler.base_crawler import BaseCrawler  # noqa: E402


class OmMpBeNlCrawler(BaseCrawler):
    """Crawler for om-mp.be NL persberichten (press releases)."""

    site_id = "om-mp-be-nl"
    site_name = "Custom: om-mp-be-nl"
    base_url = "https://www.om-mp.be"

    _LIST_URL = "https://www.om-mp.be/nl/onze-publicaties"
    _LIST_FILTER = "f%5B0%5D=type%3A2"
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_WALL_SECS = 24 * 60  # 24-minute budget (leaves 1 min margin)

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, retries=3):
        """GET via curl; returns decoded text or None after retries."""
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
            "-A", self.USER_AGENT,
            "-H", "Accept-Language: nl-BE,nl;q=0.9,en;q=0.7",
            url,
        ]
        waits = [1, 3, 9]
        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                raw = result.stdout
                if raw:
                    return raw.decode("utf-8", errors="replace")
                wait = waits[min(attempt, len(waits) - 1)]
                print(f"[{self.site_id}] Empty response (attempt {attempt+1}/{retries}), "
                      f"retrying in {wait}s: {url}")
                time.sleep(wait)
            except Exception as exc:
                wait = waits[min(attempt, len(waits) - 1)]
                print(f"[{self.site_id}] curl error (attempt {attempt+1}/{retries}): {exc}")
                if attempt < retries - 1:
                    time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_soup(html):
        """BeautifulSoup with html5lib → lxml → html.parser fallback."""
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _clean(node):
        """Normalised plain text from a BS4 node."""
        if node is None:
            return ""
        return re.sub(r"\s+", " ", node.get_text(separator=" ", strip=True)).strip()

    @staticmethod
    def _parse_date(dt_str):
        """Convert various datetime strings to YYYY-MM-DD or return ''."""
        if not dt_str:
            return ""
        dt_str = dt_str.strip()
        m = re.match(r"(\d{4}-\d{2}-\d{2})", dt_str)       # ISO
        if m:
            return m.group(1)
        m = re.match(r"(\d{2})/(\d{2})/(\d{4})", dt_str)   # DD/MM/YYYY
        if m:
            return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        m = re.match(r"(\d{2})/(\d{2})/(\d{2})$", dt_str)  # DD/MM/YY
        if m:
            y = int(m.group(3))
            return f"{2000+y if y < 50 else 1900+y}-{m.group(2)}-{m.group(1)}"
        return ""

    def _parse_listing_page(self, html):
        """Return list of article-metadata dicts from one listing page."""
        soup = self._make_soup(html)
        if not soup:
            return []

        articles = []
        for art in soup.find_all("article"):
            # Title
            h2 = art.find("h2")
            title = self._clean(h2) if h2 else ""

            # Listed date — first <time datetime="..."> in this article block
            listed_date = ""
            for t in art.find_all("time", attrs={"datetime": True}):
                listed_date = self._parse_date(t.get("datetime", ""))
                if listed_date:
                    break

            # Publisher and department from node__labels links
            publisher = ""
            department = ""
            labels = art.find("span", class_="node__labels")
            if labels:
                ll = labels.find_all("a")
                if ll:
                    publisher = self._clean(ll[0])
                if len(ll) > 1:
                    department = self._clean(ll[1])

            # Category
            cat_div = art.find(
                class_=lambda c: c and "field--name-field-blog-post-type" in c
            )
            category = self._clean(cat_div) if cat_div else ""

            # Detail URL — last /nl/artikel/ or /nl/blog-post/ link in article
            detail_path = ""
            for a in art.find_all("a", href=True):
                href = a["href"]
                if re.match(r"/nl/(artikel|blog-post)/", href):
                    detail_path = href
            if not detail_path or not title:
                continue

            articles.append({
                "title": title,
                "listed_date": listed_date,
                "publisher": publisher,
                "department": department,
                "category": category,
                "detail_path": detail_path,
            })
        return articles

    def _parse_detail_page(self, html):
        """Return (title, pub_date, abstract, pdf_url) from a detail page."""
        soup = self._make_soup(html)
        if not soup:
            return "", "", "", ""

        # Title from <h1>
        h1 = soup.find("h1")
        title = self._clean(h1) if h1 else ""

        # Published date — prefer ISO <time datetime="YYYY-MM-DDT...">
        pub_date = ""
        for t in soup.find_all("time", attrs={"datetime": True}):
            d = self._parse_date(t.get("datetime", ""))
            if d:
                pub_date = d
                break

        # Abstract — prefer field--name-field-body, fallback field--name-field-content
        abstract = ""
        for cls in ("field--name-field-body", "field--name-field-content"):
            div = soup.find(class_=lambda c: c and cls in c)
            if div:
                abstract = self._clean(div)
                if abstract:
                    break

        # PDF link
        pdf_url = ""
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if re.search(r"\.pdf(\?|#|$)", href, re.I):
                pdf_url = (self.base_url + href) if href.startswith("/") else href
                break

        return title, pub_date, abstract, pdf_url

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        saved = 0
        seen_urls = set()
        page = 0

        while True:
            # ---- hard stops ----
            if limit is not None and saved >= limit:
                break
            if page >= self._MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached.")
                break
            if time.time() - start_time > self._MAX_WALL_SECS:
                print(f"[{self.site_id}] Wall-clock budget exceeded, stopping.")
                break

            # ---- progress log every 10 pages ----
            if page > 0 and page % 10 == 0:
                limit_str = str(limit) if limit is not None else "∞"
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            # ---- fetch listing page ----
            list_url = f"{self._LIST_URL}?{self._LIST_FILTER}&page={page}"
            raw = self._curl_get(list_url)
            if not raw:
                print(f"[{self.site_id}] Failed to fetch listing page {page}, stopping.")
                break

            try:
                items = self._parse_listing_page(raw)
            except Exception as exc:
                print(f"[{self.site_id}] Listing parse error at page {page}: {exc}")
                items = []

            if not items:
                print(f"[{self.site_id}] No items at page {page}, stopping.")
                break

            # ---- URL deduplication (guard against silent page-1 loop) ----
            new_items = [it for it in items if it["detail_path"] not in seen_urls]
            if not new_items:
                print(f"[{self.site_id}] All items on page {page} already seen, stopping.")
                break
            for it in new_items:
                seen_urls.add(it["detail_path"])

            # ---- fetch detail + save ----
            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                detail_url = self.base_url + item["detail_path"]
                try:
                    time.sleep(self._delay)
                    detail_html = self._curl_get(detail_url)
                    if not detail_html:
                        print(f"[{self.site_id}] Failed to fetch detail: {detail_url}")
                        continue

                    title, pub_date, abstract, pdf_url = self._parse_detail_page(detail_html)

                    # Fall back to listing-page metadata when detail is empty
                    if not title:
                        title = item["title"]
                    if not pub_date:
                        pub_date = item["listed_date"]

                    # Skip items whose abstract is too short to be useful
                    if len(abstract) < 50:
                        print(f"[{self.site_id}] Short abstract ({len(abstract)} chars), "
                              f"skipping: {detail_url}")
                        continue

                    # External ID = URL slug (no numeric node ID exposed by this site)
                    slug = item["detail_path"].rstrip("/").split("/")[-1]

                    original_filename = None
                    if pdf_url:
                        fn = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
                        if fn:
                            original_filename = fn

                    paper = {
                        "site_id": self.site_id,
                        "external_id": slug,
                        "post_number": slug,
                        "title": title,
                        "abstract": abstract,
                        "published_date": pub_date,
                        "listed_date": item["listed_date"],
                        "authors": "",
                        "publisher": item["publisher"],
                        "department": item["department"],
                        "journal": "",
                        "url": detail_url,
                        "pdf_url": pdf_url or None,
                        "keywords": "",
                        "category": item["category"],
                        "doi": None,
                        "original_filename": original_filename,
                        "metadata": json.dumps({
                            "posted_date": item["listed_date"],
                            "slug": slug,
                            "category": item["category"],
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    limit_str = str(limit) if limit is not None else "∞"
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item.get('detail_path', '?')} failed: {exc}")
                    continue

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
