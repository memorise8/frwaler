# -*- coding: utf-8 -*-
"""Crawler for doc.cerema.fr — Archimed/Ermes v25 OPAC (RSS-based)."""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate
from html import unescape
from urllib.parse import urljoin

from crawler.base_crawler import BaseCrawler
from crawler import db as db_module


class DocCeremaFrDefaultCrawler(BaseCrawler):
    site_id = "doc-cerema-fr-default"
    site_name = "Doc Cerema"
    base_url = "https://doc.cerema.fr"

    _RSS_INDEX = "https://doc.cerema.fr/Default/tous-les-flux-rss.aspx"
    _WALL_CLOCK_BUDGET = 25 * 60  # 25 minutes in seconds

    def __init__(self, db_conn, delay=1.5):
        super().__init__(db_conn, delay)
        db_module.register_site(db_conn, self.site_id, self.site_name, self.base_url)

    # ------------------------------------------------------------------
    # RSS feed discovery
    # ------------------------------------------------------------------

    def _discover_rss_feeds(self):
        """Fetch the RSS index page and return a list of absolute feed URLs."""
        resp = self._request(self._RSS_INDEX, retries=3)
        if resp is None:
            print(f"[{self.site_id}] Failed to fetch RSS index page")
            return []

        html = resp.text
        raw_urls = re.findall(
            r'href=["\']([^"\']*SearchRss[^"\']*)["\']',
            html,
            re.IGNORECASE,
        )

        feeds = []
        seen = set()
        for url in raw_urls:
            absolute = url if url.startswith("http") else urljoin(self.base_url, url)
            if absolute not in seen:
                seen.add(absolute)
                feeds.append(absolute)

        print(f"[{self.site_id}] Discovered {len(feeds)} RSS feed(s)")
        return feeds

    # ------------------------------------------------------------------
    # RSS item parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_pubdate(raw):
        """Parse RSS pubDate string to YYYY-MM-DD, or empty string."""
        if not raw:
            return ""
        try:
            parsed = parsedate(raw.strip())
            if parsed:
                return time.strftime("%Y-%m-%d", parsed)
        except Exception:
            pass
        return ""

    @staticmethod
    def _extract_syracuse_id(url):
        """Extract numeric SYRACUSE ID from a doc.cerema.fr document URL."""
        match = re.search(r'/SYRACUSE/(\d+)', url, re.IGNORECASE)
        if match:
            return match.group(1)
        return None

    def _parse_rss_items(self, xml_text):
        """Parse RSS 2.0 XML and return list of item dicts."""
        items = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            print(f"[{self.site_id}] RSS XML parse error: {exc}")
            return items

        # Handle both namespaced and plain RSS
        ns = ""
        channel = root.find("channel")
        if channel is None:
            channel = root

        for item in channel.findall("item"):
            def _text(tag):
                el = item.find(tag)
                if el is None:
                    return ""
                return (el.text or "").strip()

            title = unescape(_text("title"))
            link = _text("link")
            pubdate = self._parse_pubdate(_text("pubDate"))

            if not link:
                continue

            syracuse_id = self._extract_syracuse_id(link)
            if not syracuse_id:
                continue

            items.append({
                "title": title,
                "url": link,
                "external_id": syracuse_id,
                "published_date": pubdate,
            })

        return items

    # ------------------------------------------------------------------
    # Abstract extraction from detail page
    # ------------------------------------------------------------------

    def _fetch_abstract(self, url):
        """Fetch detail page and extract <meta name="description"> content."""
        resp = self._request(url, retries=3)
        if resp is None:
            return ""

        html = resp.text

        # Try normal order: name first, then content
        match = re.search(
            r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']',
            html,
            re.IGNORECASE | re.DOTALL,
        )
        if not match:
            # Try reversed order: content first, then name
            match = re.search(
                r'<meta\s+content=["\'](.*?)["\']\s+name=["\']description["\']',
                html,
                re.IGNORECASE | re.DOTALL,
            )

        if match:
            abstract = unescape(match.group(1)).strip()
            # Normalize whitespace
            abstract = re.sub(r'\s+', ' ', abstract).strip()
            return abstract

        return ""

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        saved = 0
        seen_ids = set()

        feeds = self._discover_rss_feeds()
        if not feeds:
            print(f"[{self.site_id}] No RSS feeds found; aborting")
            return saved

        for feed_url in feeds:
            if limit is not None and saved >= limit:
                break

            elapsed = time.time() - start_time
            if elapsed >= self._WALL_CLOCK_BUDGET:
                print(f"[{self.site_id}] Wall-clock budget exhausted ({elapsed:.0f}s); stopping")
                break

            print(f"[{self.site_id}] Fetching feed: {feed_url}")
            resp = self._request(feed_url, retries=3)
            if resp is None:
                print(f"[{self.site_id}] Failed to fetch feed {feed_url}; skipping")
                continue

            items = self._parse_rss_items(resp.text)
            print(f"[{self.site_id}] Feed has {len(items)} item(s)")

            for item in items:
                if limit is not None and saved >= limit:
                    break

                elapsed = time.time() - start_time
                if elapsed >= self._WALL_CLOCK_BUDGET:
                    print(f"[{self.site_id}] Wall-clock budget exhausted; stopping")
                    break

                external_id = item["external_id"]
                if external_id in seen_ids:
                    continue
                seen_ids.add(external_id)

                try:
                    abstract = self._fetch_abstract(item["url"])
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] Skipping {external_id}: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    pdf_url = f"{self.base_url}/Default/digital-viewer/c-{external_id}"

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "url": item["url"],
                        "title": item["title"] or f"Document {external_id}",
                        "abstract": abstract,
                        "pdf_url": pdf_url,
                        "published_date": item.get("published_date", ""),
                        "publisher": "CEREMA",
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(
                        f"[{self.site_id}] saved {counter}: "
                        f"{paper['title'][:80]}"
                    )

                except Exception as exc:
                    print(f"[{self.site_id}] item {external_id} failed: {exc}")
                    continue

        print(f"[{self.site_id}] done. total saved: {saved}")
        return saved
