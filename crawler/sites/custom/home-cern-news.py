# -*- coding: utf-8 -*-
"""CERN News crawler — RSS-feed-based (home.cern/news).

Pagination: https://home.cern/feed/?paged=N  (10 items per page, WordPress default)
Each item carries full article HTML in <content:encoded>, giving rich abstracts
without any per-article detail-page fetch.
"""

import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from crawler.base_crawler import BaseCrawler

_NS_CONTENT = "http://purl.org/rss/1.0/modules/content/"
_NS_DC = "http://purl.org/dc/elements/1.1/"


# ---------------------------------------------------------------------------
# HTML → plain text helper
# ---------------------------------------------------------------------------

def _strip_html(html: str) -> str:
    """Extract plain text from HTML; tries html5lib → lxml → html.parser → regex."""
    if not html:
        return ""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, parser)
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            text = soup.get_text(separator=" ")
            return re.sub(r"\s+", " ", text).strip()
        except Exception:
            continue
    # Last resort: regex strip
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class CernNewsCrawler(BaseCrawler):
    """Crawler for CERN news (home.cern) via the paginated RSS feed."""

    site_id = "home-cern-news"
    site_name = "Custom: home-cern-news"
    base_url = "https://home.cern"

    _FEED_BASE = "https://home.cern/feed/"
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _WALL_CLOCK_BUDGET = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes in seconds

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, retries: int = 3) -> str | None:
        """GET url via curl with TLS flags. Returns decoded body or None."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-A", self.USER_AGENT,
            url,
        ]
        waits = [1, 3, 9]
        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                body = result.stdout
                if body:
                    return body.decode("utf-8", errors="replace")
                wait = waits[min(attempt, len(waits) - 1)]
                print(f"[{self.site_id}] Empty response from {url}, retry {attempt+1}/{retries} in {wait}s")
                time.sleep(wait)
            except subprocess.TimeoutExpired:
                wait = waits[min(attempt, len(waits) - 1)]
                print(f"[{self.site_id}] Timeout for {url}, retry {attempt+1}/{retries} in {wait}s")
                time.sleep(wait)
            except Exception as exc:
                wait = waits[min(attempt, len(waits) - 1)]
                print(f"[{self.site_id}] curl error ({exc}), retry {attempt+1}/{retries} in {wait}s")
                time.sleep(wait)
        print(f"[{self.site_id}] curl failed after {retries} attempts: {url}")
        return None

    # ------------------------------------------------------------------
    # RSS parsing
    # ------------------------------------------------------------------

    def _parse_rss_page(self, raw: str) -> list[dict]:
        """Parse one RSS page; returns list of item dicts (may be empty)."""
        # Detect non-XML response (e.g. error HTML page)
        stripped = raw.lstrip()
        if stripped and stripped[0] != "<":
            print(f"[{self.site_id}] Response does not look like XML (starts with {stripped[:20]!r})")
            return []

        try:
            root = ET.fromstring(raw.encode("utf-8"))
        except ET.ParseError as exc:
            print(f"[{self.site_id}] XML parse error: {exc}")
            return []

        channel = root.find("channel")
        if channel is None:
            return []

        items = []
        for item_el in channel.findall("item"):

            def _text(tag: str) -> str:
                el = item_el.find(tag)
                return (el.text or "").strip() if el is not None else ""

            title = _text("title")
            link = _text("link")
            pub_date_str = _text("pubDate")

            # Author from dc:creator
            creator_el = item_el.find(f"{{{_NS_DC}}}creator")
            author = (creator_el.text or "").strip() if creator_el is not None else ""

            # External ID from guid (?p=NNNN)
            guid_el = item_el.find("guid")
            guid = (guid_el.text or "").strip() if guid_el is not None else ""
            m = re.search(r"[?&]p=(\d+)", guid)
            if m:
                external_id = m.group(1)
            else:
                # Fallback: URL slug
                external_id = link.rstrip("/").split("/")[-1] or guid

            # Date parsing
            published_date = ""
            if pub_date_str:
                try:
                    dt = parsedate_to_datetime(pub_date_str)
                    published_date = dt.strftime("%Y-%m-%d")
                except Exception:
                    m2 = re.search(r"(\d{1,2})\s+(\w+)\s+(\d{4})", pub_date_str)
                    if m2:
                        try:
                            published_date = datetime.strptime(
                                f"{m2.group(1)} {m2.group(2)} {m2.group(3)}", "%d %b %Y"
                            ).strftime("%Y-%m-%d")
                        except Exception:
                            pass

            # Categories / keywords
            categories = [
                el.text.strip()
                for el in item_el.findall("category")
                if el.text and el.text.strip()
            ]

            # Abstract: content:encoded (full HTML) preferred, else description
            ce_el = item_el.find(f"{{{_NS_CONTENT}}}encoded")
            content_html = (ce_el.text or "") if ce_el is not None else ""
            desc_el = item_el.find("description")
            description_html = (desc_el.text or "") if desc_el is not None else ""

            if content_html:
                abstract = _strip_html(content_html)
            else:
                abstract = _strip_html(description_html)

            items.append({
                "external_id": external_id,
                "title": title,
                "url": link,
                "authors": [author] if author else [],
                "published_date": published_date,
                "keywords": categories,
                "abstract": abstract,
                "guid": guid,
            })

        return items

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl CERN news via paginated RSS feed.

        Parameters
        ----------
        limit:
            Max items to save. None means unlimited (up to safety cap).
        """
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        lim_display = str(limit) if limit is not None else "inf"

        for page in range(1, self._MAX_PAGES + 1):
            # Wall-clock budget
            elapsed = time.time() - start_time
            if elapsed > self._WALL_CLOCK_BUDGET:
                print(f"[{self.site_id}] Wall-clock budget exceeded ({elapsed:.0f}s). Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{lim_display}")

            feed_url = f"{self._FEED_BASE}?paged={page}"
            raw = self._curl_get(feed_url)
            if not raw:
                print(f"[{self.site_id}] Failed to fetch page {page}. Stopping.")
                break

            try:
                items = self._parse_rss_page(raw)
            except Exception as exc:
                print(f"[{self.site_id}] Error parsing page {page}: {exc}. Stopping.")
                break

            if not items:
                print(f"[{self.site_id}] No items on page {page}. Done.")
                break

            # Detect paginator loop-back (same first URL as a previously seen page)
            first_url = items[0].get("url", "")
            if first_url and first_url in seen_urls:
                print(f"[{self.site_id}] Duplicate page detected at page {page} ({first_url}). Done.")
                break

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                url = item.get("url", "")
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_on_page += 1

                try:
                    title = item.get("title", "")
                    abstract = item.get("abstract", "")
                    external_id = item.get("external_id", "")

                    if not abstract or len(abstract) < 50:
                        print(
                            f"[{self.site_id}] Skip '{title[:50]}': "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    authors = item.get("authors", [])
                    keywords = item.get("keywords", [])

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "title": title,
                        "authors": json.dumps(authors, ensure_ascii=False),
                        "abstract": abstract,
                        "category": keywords[0] if keywords else "",
                        "keywords": json.dumps(keywords, ensure_ascii=False),
                        "published_date": item.get("published_date", ""),
                        "url": url,
                        "pdf_url": "",
                        "doi": "",
                        "department": "CERN",
                        "metadata": json.dumps(
                            {"guid": item.get("guid", ""), "categories": keywords},
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{lim_display}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {url} failed: {exc}")
                    continue

                time.sleep(self._delay)

            if new_on_page == 0:
                print(f"[{self.site_id}] All items on page {page} already seen. Done.")
                break

            if page == self._MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
