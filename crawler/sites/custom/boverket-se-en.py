# -*- coding: utf-8 -*-
"""Boverket (English) publications crawler.

Starting URL: https://www.boverket.se/en/start/publications/
Uses the RSS feed for listing, then fetches each detail page for full content.
"""

import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from crawler.base_crawler import BaseCrawler  # noqa: E402

_SITE_ID = "boverket-se-en"
_BASE_URL = "https://www.boverket.se"
_RSS_URL = f"{_BASE_URL}/en/start/publications/?format=rss"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _curl_get(url: str, retries: int = 3) -> str | None:
    """Fetch URL via curl with exponential-backoff retries."""
    delays = [1, 3, 9]
    for attempt in range(retries):
        try:
            result = subprocess.run(
                [
                    "curl", "--tls-max", "1.3", "-sk",
                    "--max-time", "30",
                    "-H", f"User-Agent: {_UA}",
                    "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    url,
                ],
                capture_output=True,
                timeout=35,
            )
            raw = result.stdout
            if raw:
                try:
                    return raw.decode("utf-8", errors="replace")
                except Exception:
                    return raw.decode("latin-1", errors="replace")
        except Exception as exc:
            print(f"[{_SITE_ID}] curl error attempt {attempt + 1}: {exc}")

        if attempt < retries - 1:
            wait = delays[attempt]
            print(f"[{_SITE_ID}] Retrying in {wait}s…")
            time.sleep(wait)

    return None


def _make_soup(html: str):
    """Build BeautifulSoup with fallback: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup  # noqa: PLC0415
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class BoverketSeEnCrawler(BaseCrawler):
    """Crawler for Boverket Swedish Board of Housing – English publications."""

    site_id = "boverket-se-en"
    site_name = "Custom: boverket-se-en"
    base_url = "https://www.boverket.se"

    def crawl(self, limit=None):
        """Crawl Boverket English publications (RSS listing + HTML detail pages).

        Parameters
        ----------
        limit:
            Maximum number of newly stored records. ``None`` has no row limit.
        """
        self._check_cancelled()
        if limit is not None and limit <= 0:
            return 0
        start_time = time.time()
        MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

        saved = 0
        seen_urls: set[str] = set()
        limit_str = str(limit) if limit is not None else "∞"

        # ------------------------------------------------------------------
        # Feed membership is observed here; archive completeness is unverified.
        # ------------------------------------------------------------------
        rss_raw = _curl_get(_RSS_URL)
        if not rss_raw:
            self._report_source_failure('fetch_failed')
            print(f"[{self.site_id}] Failed to fetch RSS listing. Aborting.")
            return saved

        items = []
        try:
            root = ET.fromstring(rss_raw)
            channel = root.find("channel")
            if root.tag != 'rss' or channel is None:
                self._report_source_failure('invalid_response')
                return saved
            if channel is not None:
                for el in channel.findall("item"):
                    def _t(tag, _el=el):
                        node = _el.find(tag)
                        return node.text.strip() if node is not None and node.text else ""
                    items.append({
                        "guid":      _t("guid"),
                        "url":       _t("link"),
                        "title":     _t("title"),
                        "rss_desc":  _t("description"),
                        "pub_date":  _t("pubDate"),
                    })
        except ET.ParseError as exc:
            self._report_source_failure('invalid_response')
            print(f"[{self.site_id}] RSS parse error: {exc}. Aborting.")
            return saved

        if not items:
            print(f"[{self.site_id}] RSS feed returned 0 items.")
            return saved

        print(f"[{self.site_id}] Found {len(items)} item(s) in RSS feed.")

        # ------------------------------------------------------------------
        # Step 2: Paginated detail-fetch loop.
        # Boverket's feed is a single "page". We still honour the safety cap
        # and detect end-of-pagination via seen_urls / empty items.
        # ------------------------------------------------------------------
        page = 1
        batch = items  # page 1 = all RSS items
        MAX_PAGES = 200

        while batch:
            if page > MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {MAX_PAGES} pages reached; stopping.")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            new_on_page = 0

            for item in batch:
                self._check_cancelled()
                if limit is not None and saved >= limit:
                    break

                if time.time() - start_time > MAX_WALL_SECONDS:
                    print(f"[{self.site_id}] 25-min wall-clock budget reached; stopping cleanly.")
                    batch = []
                    break

                url = item.get("url", "")
                if not url:
                    continue
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_on_page += 1

                # Per-item failure isolation
                try:
                    ok = self._fetch_and_save(item)
                    if ok:
                        saved += 1
                        print(f"[{self.site_id}] Saved {saved}/{limit_str}: {item['title'][:60]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    self._report_source_failure('item_failed')
                    print(f"[{self.site_id}] item {url} failed: {exc}; continuing")
                    continue

                time.sleep(self._delay)

            # End-of-pagination: no unseen URLs on this page → done.
            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: no new records. Done.")
                break

            # For Boverket there is only one RSS page; subsequent pages = empty.
            page += 1
            batch = []  # no further pages for this site

        print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")
        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Pure enumeration entry points; crawl() and _fetch_and_save stay unchanged.
    # ------------------------------------------------------------------

    def _enumeration_items(self, raw, url=None):
        """RSS items exactly as crawl() reads them; non-RSS answers are listing errors."""
        from crawler.enumeration.http import PermanentFailure
        try:
            root = ET.fromstring(raw)
        except (ET.ParseError, TypeError):
            raise PermanentFailure("listing_parse_error:invalid_xml", url) from None
        channel = root.find("channel")
        if root.tag != 'rss' or channel is None:
            raise PermanentFailure("listing_parse_error:not_an_rss_channel", url)
        items = []
        for el in channel.findall("item"):
            def _t(tag, _el=el):
                node = _el.find(tag)
                return node.text.strip() if node is not None and node.text else ""
            items.append({
                "guid":      _t("guid"),
                "url":       _t("link"),
                "title":     _t("title"),
                "rss_desc":  _t("description"),
                "pub_date":  _t("pubDate"),
            })
        return items

    def _enumeration_paper(self, raw_html, item):
        """Pure copy of _fetch_and_save's parsing and Paper conversion (None = skipped)."""
        from crawler.enumeration.http import PermanentFailure
        url = item["url"]
        guid = item["guid"]
        title = item["title"]
        rss_desc = item["rss_desc"]
        pub_date = item["pub_date"]

        if not raw_html:
            raise PermanentFailure("detail_empty_response", url)
        soup = _make_soup(raw_html)
        if soup is None:
            raise PermanentFailure("detail_parse_failed", url)

        abstract = ""
        if soup:
            main_body = soup.find("div", class_="main-body")
            if main_body:
                paras = [
                    p.get_text(separator=" ", strip=True)
                    for p in main_body.find_all("p")
                    if p.get_text(strip=True)
                ]
                abstract = "\n\n".join(paras)

        if len(abstract.strip()) < 50:
            og_m = re.search(r'<meta property="og:description" content="([^"]+)"', raw_html)
            if og_m:
                abstract = og_m.group(1)

        if len(abstract.strip()) < 50:
            abstract = rss_desc

        abstract = abstract.strip()

        if len(abstract) < 100:
            return None

        pdf_url = ""
        if soup:
            pdf_tag = soup.find("a", rel="attachment")
            if pdf_tag and pdf_tag.get("href"):
                href = pdf_tag["href"]
                pdf_url = f"{self.base_url}{href}" if href.startswith("/") else href

        if not pdf_url:
            m = re.search(r'href="(/globalassets/[^"]+\.pdf)"', raw_html)
            if m:
                pdf_url = f"{self.base_url}{m.group(1)}"

        pub_meta: dict = {}
        if soup:
            pub_metadata_div = soup.find("div", class_="publication-metadata")
            if pub_metadata_div:
                for row in pub_metadata_div.find_all("div", class_="metadata-row"):
                    lbl = row.find("div", class_="metadata-label")
                    val = row.find("div", class_="metadata-value")
                    if lbl and val:
                        k = lbl.get_text(strip=True).rstrip(":")
                        v = val.get_text(strip=True)
                        if k and v:
                            pub_meta[k] = v

        if not pub_date and pub_meta.get("Year"):
            pub_date = pub_meta['Year']

        external_id = guid if guid else url.rstrip("/").split("/")[-2]

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": external_id,
            "title": title,
            "authors": json.dumps([], ensure_ascii=False),
            "abstract": abstract,
            "category": "",
            "keywords": json.dumps([], ensure_ascii=False),
            "published_date": pub_date,
            "url": url,
            "pdf_url": pdf_url,
            "doi": "",
            "department": "Boverket",
            "metadata": json.dumps(pub_meta, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Detail-page fetch + save
    # ------------------------------------------------------------------

    def _fetch_and_save(self, item: dict) -> bool:
        """Fetch and save a detail; return whether a new database row was made."""
        url = item["url"]
        guid = item["guid"]
        title = item["title"]
        rss_desc = item["rss_desc"]
        pub_date = item["pub_date"]

        raw_html = _curl_get(url)
        if not raw_html:
            self._report_source_failure('item_failed')
            print(f"[{self.site_id}] Could not fetch detail page: {url}")
            return False

        # --- Parse ---
        soup = None
        try:
            soup = _make_soup(raw_html)
        except Exception as exc:
            print(f"[{self.site_id}] BeautifulSoup failed for {url}: {exc}")
        if soup is None:
            self._report_source_failure('item_failed')
            return False

        # --- Abstract: main-body paragraphs (full text) ---
        abstract = ""
        if soup:
            main_body = soup.find("div", class_="main-body")
            if main_body:
                paras = [
                    p.get_text(separator=" ", strip=True)
                    for p in main_body.find_all("p")
                    if p.get_text(strip=True)
                ]
                abstract = "\n\n".join(paras)

        # Fallback 1: og:description meta tag
        if len(abstract.strip()) < 50:
            og_m = re.search(r'<meta property="og:description" content="([^"]+)"', raw_html)
            if og_m:
                abstract = og_m.group(1)

        # Fallback 2: RSS description
        if len(abstract.strip()) < 50:
            abstract = rss_desc

        abstract = abstract.strip()

        # Skip items with insufficient abstract (would fail DB quality check)
        if len(abstract) < 100:
            print(
                f"[{self.site_id}] Skipping '{title[:55]}' — abstract too short "
                f"({len(abstract)} chars)"
            )
            return False

        # --- PDF URL ---
        pdf_url = ""
        if soup:
            pdf_tag = soup.find("a", rel="attachment")
            if pdf_tag and pdf_tag.get("href"):
                href = pdf_tag["href"]
                pdf_url = f"{self.base_url}{href}" if href.startswith("/") else href

        if not pdf_url:
            m = re.search(r'href="(/globalassets/[^"]+\.pdf)"', raw_html)
            if m:
                pdf_url = f"{self.base_url}{m.group(1)}"

        # --- Publication metadata (Title, Year, Report No., ISBN, etc.) ---
        pub_meta: dict = {}
        if soup:
            pub_metadata_div = soup.find("div", class_="publication-metadata")
            if pub_metadata_div:
                for row in pub_metadata_div.find_all("div", class_="metadata-row"):
                    lbl = row.find("div", class_="metadata-label")
                    val = row.find("div", class_="metadata-value")
                    if lbl and val:
                        k = lbl.get_text(strip=True).rstrip(":")
                        v = val.get_text(strip=True)
                        if k and v:
                            pub_meta[k] = v

        # Supplement published_date from metadata Year if RSS date missing
        if not pub_date and pub_meta.get("Year"):
            pub_date = pub_meta['Year']

        external_id = guid if guid else url.rstrip("/").split("/")[-2]

        paper = {
            "id": None,
            "site_id": self.site_id,
            "external_id": external_id,
            "title": title,
            "authors": json.dumps([], ensure_ascii=False),
            "abstract": abstract,
            "category": "",
            "keywords": json.dumps([], ensure_ascii=False),
            "published_date": pub_date,
            "url": url,
            "pdf_url": pdf_url,
            "doi": "",
            "department": "Boverket",
            "metadata": json.dumps(pub_meta, ensure_ascii=False),
        }

        self._save_paper(paper)
        return self._last_save_created
