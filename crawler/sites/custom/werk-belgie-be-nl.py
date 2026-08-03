# -*- coding: utf-8 -*-
"""Crawler for werk.belgie.be/nl/nieuws (Belgian Federal Employment Service).

Starting URL: https://werk.belgie.be/nl/nieuws
Drupal 10 listing, pagination: ?page=N (0-based, 10 items/page, ~62 pages)
Detail URLs:  https://werk.belgie.be/nl/news/<slug>
Abstract:     full body text from detail page field--name-body
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


class WerkBelgieBeNlCrawler(BaseCrawler):
    """Crawler for werk.belgie.be/nl/nieuws (FOD Werkgelegenheid news)."""

    site_id = "werk-belgie-be-nl"
    site_name = "Custom: werk-belgie-be-nl"
    base_url = "https://werk.belgie.be"

    _LIST_URL = "https://werk.belgie.be/nl/nieuws"
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_WALL_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute budget

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, retries=3):
        """GET via curl with TLS-max and retry/backoff. Returns text or None."""
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
            "-A", self.USER_AGENT,
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: nl-NL,nl;q=0.9",
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
        """Normalised plain text from a BS4 tag or string."""
        if node is None:
            return ""
        if hasattr(node, "get_text"):
            text = node.get_text(separator=" ", strip=True)
        else:
            text = str(node)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _parse_date(dt_str):
        """Convert datetime string to YYYY-MM-DD or return ''."""
        if not dt_str:
            return ""
        dt_str = dt_str.strip()
        m = re.match(r"(\d{4}-\d{2}-\d{2})", dt_str)
        if m:
            return m.group(1)
        return ""

    # ------------------------------------------------------------------
    # Listing page parser
    # ------------------------------------------------------------------

    def _parse_listing(self, html):
        """Return list of item-dicts from one listing page HTML."""
        soup = self._make_soup(html)
        if not soup:
            return []

        items = []
        for li in soup.select("li.views-row"):
            art = li.find("article")
            if not art:
                continue

            # Title + detail URL
            title_div = art.find(class_=lambda c: c and "field--name-node-title" in c)
            if not title_div:
                continue
            a_tag = title_div.find("a", href=True)
            if not a_tag:
                continue
            rel_path = a_tag.get("href", "")
            if not rel_path:
                continue
            detail_url = (
                self.base_url + rel_path if rel_path.startswith("/") else rel_path
            )
            title = self._clean(a_tag)
            slug = rel_path.rstrip("/").split("/")[-1]

            # Listed date — first <time datetime> in the day field
            listed_date = ""
            day_div = art.find(
                class_=lambda c: c and "field--name-dynamic-token-fieldnode-publication-day" in c
            )
            if day_div:
                t = day_div.find("time", attrs={"datetime": True})
                if t:
                    listed_date = self._parse_date(t.get("datetime", ""))
            if not listed_date:
                t = art.find("time", attrs={"datetime": True})
                if t:
                    listed_date = self._parse_date(t.get("datetime", ""))

            # Published date (may differ from listed_date)
            pub_date = ""
            pub_div = art.find(
                class_=lambda c: c and "field--name-field-publication-date" in c
            )
            if pub_div:
                t = pub_div.find("time", attrs={"datetime": True})
                if t:
                    pub_date = self._parse_date(t.get("datetime", ""))
            if not pub_date:
                pub_date = listed_date

            # Category / theme
            category = ""
            theme_div = art.find(
                class_=lambda c: c and "field--name-field-theme" in c
            )
            if theme_div:
                cat_a = theme_div.find("a")
                if cat_a:
                    category = self._clean(cat_a)

            # Teaser body from listing (fallback if detail fetch fails)
            teaser = ""
            body_div = art.find(class_=lambda c: c and "field--name-body" in c)
            if body_div:
                teaser = self._clean(body_div)

            items.append({
                "slug": slug,
                "title": title,
                "detail_url": detail_url,
                "listed_date": listed_date,
                "pub_date": pub_date,
                "category": category,
                "teaser": teaser,
            })

        return items

    # ------------------------------------------------------------------
    # Detail page parser
    # ------------------------------------------------------------------

    def _parse_detail(self, html, item):
        """Fetch full body text and extra fields from detail page HTML."""
        soup = self._make_soup(html)
        if not soup:
            return None

        # Full body
        abstract = ""
        body_div = soup.find(class_=lambda c: c and "field--name-body" in c)
        if body_div:
            abstract = self._clean(body_div)

        # Refine pub_date from detail page
        pub_date = item["pub_date"]
        pub_div = soup.find(class_=lambda c: c and "field--name-field-publication-date" in c)
        if pub_div:
            t = pub_div.find("time", attrs={"datetime": True})
            if t:
                d = self._parse_date(t.get("datetime", ""))
                if d:
                    pub_date = d

        # Category
        category = item["category"]
        if not category:
            theme_div = soup.find(class_=lambda c: c and "field--name-field-theme" in c)
            if theme_div:
                cat_a = theme_div.find("a")
                if cat_a:
                    category = self._clean(cat_a)

        # PDF URL — look in body for any PDF link or /sites/default/files/ link
        pdf_url = None
        original_filename = None
        if body_div:
            for a in body_div.find_all("a", href=True):
                href = a.get("href", "")
                lower = href.lower()
                if lower.endswith(".pdf") or "/sites/default/files/" in lower:
                    pdf_url = href if href.startswith("http") else self.base_url + href
                    tail = href.rstrip("/").split("/")[-1].split("?")[0]
                    if tail and "." in tail:
                        original_filename = tail
                    break

        # Keywords from meta tag
        keywords = ""
        meta_kw = soup.find("meta", attrs={"name": "keywords"})
        if meta_kw:
            keywords = meta_kw.get("content", "").strip()

        return {
            "abstract": abstract,
            "pub_date": pub_date,
            "category": category,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "keywords": keywords,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 0
        seen_urls = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        while True:
            # Wall-clock budget
            if time.time() - start_time > self._MAX_WALL_SECS:
                print(f"[{self.site_id}] Wall-clock budget exceeded at page {page}, stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page >= self._MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached, stopping.")
                break

            list_url = f"{self._LIST_URL}?page={page}"
            html = self._curl_get(list_url)
            if not html:
                print(f"[{self.site_id}] Failed to fetch page {page}, stopping.")
                break

            items = self._parse_listing(html)
            if not items:
                print(f"[{self.site_id}] No items on page {page}, done.")
                break

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                detail_url = item["detail_url"]
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                try:
                    time.sleep(self._delay)
                    detail_html = self._curl_get(detail_url)

                    abstract = item["teaser"]
                    pub_date = item["pub_date"]
                    category = item["category"]
                    pdf_url = None
                    original_filename = None
                    keywords = ""

                    if detail_html:
                        detail = self._parse_detail(detail_html, item)
                        if detail:
                            abstract = detail["abstract"] or abstract
                            pub_date = detail["pub_date"] or pub_date
                            category = detail["category"] or category
                            pdf_url = detail["pdf_url"]
                            original_filename = detail["original_filename"]
                            keywords = detail["keywords"]

                    if len(abstract) < 50:
                        print(f"[{self.site_id}] Short abstract ({len(abstract)} chars) "
                              f"for {item['slug']}, skipping.")
                        continue

                    paper = {
                        "site_id": self.site_id,
                        "external_id": item["slug"],
                        "post_number": item["slug"],
                        "title": item["title"],
                        "abstract": abstract,
                        "published_date": pub_date,
                        "posted_date": item["listed_date"],
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "keywords": keywords,
                        "category": category,
                        "authors": "",
                        "publisher": (
                            "Federale Overheidsdienst Werkgelegenheid, "
                            "Arbeid en Sociaal Overleg"
                        ),
                        "department": "",
                        "journal": "",
                        "doi": "",
                        "metadata": json.dumps({
                            "posted_date": item["listed_date"],
                            "slug": item["slug"],
                            "category": category,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_str}: "
                          f"{item['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item.get('slug', '?')} failed: {exc}")
                    continue

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            if new_on_page == 0:
                print(f"[{self.site_id}] All items on page {page} already seen, done.")
                break

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
