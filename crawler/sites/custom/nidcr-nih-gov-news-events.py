# -*- coding: utf-8 -*-
"""Crawler for NIDCR News (National Institute of Dental and Craniofacial Research).

Starting URL: https://www.nidcr.nih.gov/news-events/nidcr-news

Structure:
- Current year: /news-events/nidcr-news
- Archive years: /news-events/nidcr-news/2025, /2024, /2023, /2022, /2021
- Each year page lists all articles for that year
- Detail page: individual article URL (slug-based)
- Node ID extracted from Drupal settings JSON embedded in every page
"""

import json
import os
import re
import subprocess
import time
import sys

sys.path.insert(0, '.')
from crawler.base_crawler import BaseCrawler

_SITE_ID = "nidcr-nih-gov-news-events"
_BASE_URL = "https://www.nidcr.nih.gov"
_LIST_URL = "https://www.nidcr.nih.gov/news-events/nidcr-news"
# Years available on the site (current + archived)
_ARCHIVE_YEARS = [2021, 2022, 2023, 2024, 2025]
_CURRENT_YEAR = 2026
_RATE_SLEEP = 1.0
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))  # safety cap (one "page" = one year listing here)


def _curl_get(url: str, retries: int = 3) -> str | None:
    """Fetch URL via curl with exponential backoff. Returns raw text or None."""
    cmd = [
        "curl", "--tls-max", "1.3", "-skL",
        "--max-time", "30",
        "-H", "Accept: text/html,*/*",
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            text = result.stdout.decode("utf-8", errors="replace")
            if text.strip():
                return text
            if attempt < retries - 1:
                wait = 3 ** attempt
                print(f"[{_SITE_ID}] Empty response for {url}, retry in {wait}s...")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 3 ** attempt
                print(f"[{_SITE_ID}] curl error ({url}): {exc}, retry in {wait}s...")
                time.sleep(wait)
            else:
                print(f"[{_SITE_ID}] curl failed after {retries} attempts: {url}: {exc}")
    return None


def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _extract_node_id(html: str) -> str | None:
    """Extract Drupal node ID from the embedded settings JSON."""
    m = re.search(r'data-drupal-selector="drupal-settings-json">(\{.*?\})</script>', html, re.DOTALL)
    if not m:
        return None
    try:
        settings = json.loads(m.group(1))
        current_path = settings.get("path", {}).get("currentPath", "")
        node_m = re.match(r"node/(\d+)$", current_path)
        if node_m:
            return node_m.group(1)
    except (json.JSONDecodeError, AttributeError):
        pass
    return None


def _extract_date(html: str) -> str | None:
    """Extract ISO date from <time datetime="..."> tag."""
    m = re.search(r'<time[^>]*datetime="(\d{4}-\d{2}-\d{2})', html)
    if m:
        return m.group(1)
    return None


def _extract_title(soup) -> str:
    """Extract article title from <h1> or <title> tag."""
    if soup is None:
        return ""
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(separator=" ", strip=True)
    title_tag = soup.find("title")
    if title_tag:
        raw = title_tag.get_text(strip=True)
        # Strip " | NIDCR" suffix
        return re.sub(r"\s*\|\s*NIDCR.*$", "", raw).strip()
    return ""


def _extract_abstract(soup) -> str:
    """Extract main article body text, excluding boilerplate."""
    if soup is None:
        return ""
    # The main article element
    article = soup.find("article")
    if not article:
        return ""
    # Remove unwanted sub-elements: nav, social share, sidebar, scripts, styles
    for tag in article.find_all(["nav", "script", "style", "noscript"]):
        tag.decompose()
    for tag in article.find_all(class_=re.compile(r"social|share|breadcrumb|sidebar|c-skiplinks")):
        tag.decompose()
    # Get all paragraph text
    paragraphs = []
    for el in article.find_all(["p", "li", "h2", "h3", "h4"]):
        text = el.get_text(separator=" ", strip=True)
        if text and len(text) > 10:
            paragraphs.append(text)
    abstract = " ".join(paragraphs)
    abstract = re.sub(r"\s+", " ", abstract).strip()
    return abstract


def _extract_article_links(html: str) -> list[str]:
    """Extract article URLs from a year-listing page."""
    # Match links to individual articles (not year pages, not the base listing)
    raw_links = re.findall(r'href="(/news-events/nidcr-news/[^"#?]+)"', html)
    result = []
    seen = set()
    year_re = re.compile(r"^/news-events/nidcr-news/\d{4}/?$")
    base_re = re.compile(r"^/news-events/nidcr-news/?$")
    for link in raw_links:
        link = link.rstrip("/")
        if year_re.match(link) or base_re.match(link):
            continue
        if link not in seen:
            seen.add(link)
            result.append(link)
    return result


class NIDCRNewsEventsCrawler(BaseCrawler):
    """Crawler for NIDCR News articles."""

    site_id = _SITE_ID
    site_name = "Custom: nidcr-nih-gov-news-events"
    base_url = _BASE_URL

    def crawl(self, limit=None):
        """Crawl NIDCR News articles from current year backwards through archives.

        Walks year pages (newest first), collects article URLs, then fetches
        each detail page. Stops when limit is reached.
        """
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        max_wall_seconds = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute budget

        # Build list of year pages to walk (current year first, then archives newest→oldest)
        year_pages = [(_CURRENT_YEAR, _LIST_URL)]
        for yr in sorted(_ARCHIVE_YEARS, reverse=True):
            year_pages.append((yr, f"{_BASE_URL}/news-events/nidcr-news/{yr}"))

        article_urls: list[str] = []

        # Phase 1: collect all article URLs from all year pages (up to limit need)
        pages_walked = 0
        for year, page_url in year_pages:
            if limit is not None and len(article_urls) >= limit:
                break
            if pages_walked >= _MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached. Stopping page walk.")
                break

            print(f"[{_SITE_ID}] Fetching year page {year}: {page_url}")
            html = _curl_get(page_url)
            pages_walked += 1

            if not html:
                print(f"[{_SITE_ID}] Failed to fetch year page {year}. Skipping.")
                continue

            links = _extract_article_links(html)
            print(f"[{_SITE_ID}] Year {year}: found {len(links)} article links")

            for link in links:
                full_url = f"{_BASE_URL}{link}"
                if full_url not in seen_urls:
                    seen_urls.add(full_url)
                    article_urls.append(full_url)

            time.sleep(_RATE_SLEEP)

        print(f"[{_SITE_ID}] Total unique article URLs collected: {len(article_urls)}")

        # Phase 2: fetch each article detail page
        for i, art_url in enumerate(article_urls):
            if limit is not None and saved >= limit:
                break

            # Wall-clock budget check
            elapsed = time.time() - start_time
            if elapsed > max_wall_seconds:
                print(f"[{_SITE_ID}] Wall-clock budget of 25min reached after {saved} saved. Exiting.")
                break

            if i > 0 and i % 10 == 0:
                limit_str = str(limit) if limit is not None else "∞"
                print(f"[{_SITE_ID}] page {i}: saved {saved}/{limit_str}")

            try:
                self._fetch_and_save_article(art_url)
                saved += 1
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] item {art_url} failed: {exc}")
                continue

            time.sleep(_RATE_SLEEP)

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved

    def _fetch_and_save_article(self, url: str) -> None:
        """Fetch one article page and persist it. Raises on unrecoverable error."""
        html = _curl_get(url, retries=3)
        if not html:
            raise RuntimeError(f"Failed to fetch {url}")

        soup = _make_soup(html)

        # Node ID → post_number and external_id
        node_id = _extract_node_id(html)

        # Title
        title = _extract_title(soup)
        if not title:
            title = "(untitled)"

        # Dates
        published_date = _extract_date(html)

        # Abstract from article body
        abstract = _extract_abstract(soup)

        # og:description as fallback/supplement
        og_desc_m = re.search(r'property="og:description" content="([^"]+)"', html)
        og_desc = og_desc_m.group(1).strip() if og_desc_m else ""

        # If body abstract is thin, try og:description
        if len(abstract) < 100 and og_desc:
            abstract = og_desc

        # Skip items with very short abstracts
        if len(abstract) < 50:
            print(f"[{_SITE_ID}] Skipping (abstract too short, {len(abstract)} chars): {url}")
            return

        # Slug-based external_id as fallback when no node ID
        slug = url.split("/news-events/nidcr-news/")[-1].strip("/")
        external_id = node_id if node_id else slug
        post_number = node_id  # numeric string like "27986"

        paper = {
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": published_date,
            "url": url,
            "pdf_url": None,
            "authors": None,
            "publisher": "National Institute of Dental and Craniofacial Research",
            "department": None,
            "journal": None,
            "keywords": None,
            "category": "NIDCR News",
            "doi": None,
            "original_filename": None,
            "metadata": json.dumps({
                "posted_date": published_date,
                "node_id": node_id,
                "slug": slug,
                "og_description": og_desc,
            }, ensure_ascii=False),
        }

        self._save_paper(paper)
        print(f"[{_SITE_ID}] Saved: {title[:70]}")
