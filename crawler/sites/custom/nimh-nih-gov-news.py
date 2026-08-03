# -*- coding: utf-8 -*-
"""NIMH NIH Science Updates crawler."""

import json
import re
import subprocess
import time
from datetime import datetime

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from crawler.base_crawler import BaseCrawler

_SITE_ID = "nimh-nih-gov-news"
_BASE_URL = "https://www.nimh.nih.gov"
_LIST_URL = "https://www.nimh.nih.gov/news/science-updates"
_DELAY = 1.0
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))   # safety cap (year pages)
_BUDGET_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 min wall-clock budget


def _bs(html):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    try:
        from bs4 import BeautifulSoup
        try:
            return BeautifulSoup(html, "html5lib")
        except Exception:
            pass
        try:
            return BeautifulSoup(html, "lxml")
        except Exception:
            pass
        return BeautifulSoup(html, "html.parser")
    except Exception as exc:
        raise RuntimeError(f"BeautifulSoup failed: {exc}") from exc


def _curl_get(url, retries=3):
    """Fetch URL via curl; returns text or None. Retries with backoff."""
    cmd = [
        "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
        "-H", "Accept: text/html,*/*;q=0.8",
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=40)
            text = result.stdout.decode("utf-8", errors="replace")
            if text.strip():
                return text
            if attempt < retries - 1:
                wait = (attempt + 1) ** 2
                print(f"[{_SITE_ID}] Empty response for {url}, retry in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = (attempt + 1) ** 2
                print(f"[{_SITE_ID}] curl error {url}: {exc}, retry in {wait}s")
                time.sleep(wait)
            else:
                print(f"[{_SITE_ID}] curl failed after {retries} attempts: {url}: {exc}")
    return None


def _strip_text(s):
    """Normalize whitespace in string."""
    if not s:
        return ""
    s = re.sub(r"[\xa0​­]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _parse_year_page(html):
    """Return list of dicts: {url, title, date, category, summary}."""
    try:
        soup = _bs(html)
    except Exception as exc:
        print(f"[{_SITE_ID}] Failed to parse year page: {exc}")
        return []

    items = []
    for container in soup.find_all("div", class_="aggregated_container"):
        try:
            link_el = container.find("a", class_="aggregated_term_news_link")
            if not link_el:
                continue
            href = link_el.get("href", "")
            if not href:
                continue
            url = _BASE_URL + href if href.startswith("/") else href
            title = _strip_text(link_el.get_text())

            # Date and category from <time> element
            date_str = ""
            category = ""
            time_el = container.find("time")
            if time_el:
                date_str = time_el.get("datetime", "")
                # Text is like "April 9, 2025•Research Highlight"
                full_text = _strip_text(time_el.parent.get_text())
                if "•" in full_text:
                    category = _strip_text(full_text.split("•", 1)[1])

            # Short summary from listing
            summary_el = container.find("div", class_="aggregated_desc_news_summary")
            summary = _strip_text(summary_el.get_text()) if summary_el else ""

            items.append({
                "url": url,
                "title": title,
                "date": date_str,
                "category": category,
                "summary": summary,
            })
        except Exception as exc:
            print(f"[{_SITE_ID}] Error parsing listing item: {exc}")
            continue
    return items


def _fetch_article_body(html):
    """Extract full article text from detail page HTML."""
    try:
        soup = _bs(html)
    except Exception as exc:
        print(f"[{_SITE_ID}] Failed to parse article: {exc}")
        return ""

    main = soup.find("main")
    if not main:
        return ""

    article = main.find("article", id="main_content_inner")
    if not article:
        article = main.find("article")
    if not article:
        article = main

    # Collect all paragraph and list-item text
    parts = []
    for el in article.find_all(["p", "li", "h2", "h3", "h4"]):
        text = _strip_text(el.get_text())
        if not text:
            continue
        # Skip the date/category stamp paragraph
        if re.match(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d", text):
            continue
        parts.append(text)

    return " ".join(parts)


class NimhNihGovNewsCrawler(BaseCrawler):
    """Crawler for NIMH NIH Science Updates."""

    site_id = _SITE_ID
    site_name = "Custom: nimh-nih-gov-news"
    base_url = _BASE_URL

    def crawl(self, limit=None):
        """Crawl NIMH science updates year pages and save articles.

        Walks year pages from current year backwards. Fetches each article
        detail page for the full abstract. Stops when limit is reached,
        all years are exhausted, or the 25-minute wall-clock budget expires.
        """
        saved = 0
        seen_urls = set()
        start_time = time.time()
        limit_or_inf = limit if limit is not None else float("inf")

        current_year = datetime.now().year
        year = current_year
        page_count = 0  # year pages fetched
        consecutive_empty = 0

        while saved < limit_or_inf:
            # Wall-clock budget check
            elapsed = time.time() - start_time
            if elapsed >= _BUDGET_SECS:
                print(f"[{_SITE_ID}] Wall-clock budget ({_BUDGET_SECS}s) reached after {saved} saved. Exiting.")
                break

            # Safety cap on pages
            if page_count >= _MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} year pages reached. Exiting.")
                break

            year_url = f"{_LIST_URL}/{year}"
            print(f"[{_SITE_ID}] Fetching year page {year} (saved {saved}/{limit_or_inf})")

            raw = _curl_get(year_url)
            page_count += 1

            if not raw or "aggregated_term_news_link" not in raw:
                consecutive_empty += 1
                print(f"[{_SITE_ID}] Year {year}: no articles (empty_streak={consecutive_empty})")
                if consecutive_empty >= 3:
                    print(f"[{_SITE_ID}] 3 consecutive empty years — stopping year walk.")
                    break
                year -= 1
                continue

            consecutive_empty = 0
            items = _parse_year_page(raw)

            if not items:
                year -= 1
                continue

            for item in items:
                if saved >= limit_or_inf:
                    break

                url = item["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                try:
                    self._process_article(item, saved, limit_or_inf)
                    saved += 1
                    if saved % 10 == 0:
                        print(f"[{_SITE_ID}] Progress: saved {saved}/{limit_or_inf}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] Item {url} failed: {exc}")
                    continue

            year -= 1

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved

    def _process_article(self, item, saved_so_far, limit_or_inf):
        """Fetch article detail page and save. Raises on unrecoverable errors."""
        url = item["url"]
        title = item["title"]
        date_str = item["date"]
        category = item["category"]
        listing_summary = item["summary"]

        time.sleep(_DELAY)

        raw = _curl_get(url, retries=3)
        if not raw:
            print(f"[{_SITE_ID}] Skipping (no response): {url}")
            return

        abstract = _fetch_article_body(raw)

        # Fall back to listing summary if full body extraction failed
        if not abstract or len(abstract) < 50:
            abstract = listing_summary

        # Skip if abstract is still too short
        if len(abstract) < 50:
            print(f"[{_SITE_ID}] Skipping (abstract <50 chars): {title[:60]}")
            return

        # Build external_id from URL slug
        slug = url.rstrip("/").split("/")[-1]

        paper = {
            "id": None,
            "site_id": self.site_id,
            "external_id": slug,
            "title": title,
            "authors": json.dumps([], ensure_ascii=False),
            "abstract": abstract,
            "category": category,
            "keywords": json.dumps([], ensure_ascii=False),
            "published_date": date_str,
            "url": url,
            "pdf_url": "",
            "doi": "",
            "department": "National Institute of Mental Health",
            "metadata": json.dumps({
                "listing_summary": listing_summary,
                "year_page": date_str[:4] if date_str else "",
            }, ensure_ascii=False),
        }

        self._save_paper(paper)
        counter = f"{saved_so_far + 1}/{limit_or_inf}"
        print(f"[{_SITE_ID}] Saved {counter}: {title[:60]}")
