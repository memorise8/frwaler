# -*- coding: utf-8 -*-
"""Department of Health Northern Ireland — News crawler.

Starting URL: https://www.health-ni.gov.uk/news
Uses RSS feed: https://www.health-ni.gov.uk/news/feed/health?page=N
(100 items/page, Drupal RSS with pagination)
"""

import json
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_BASE = "https://www.health-ni.gov.uk"
_SITE_ID = "health-ni-gov-uk-news"
_PUBLISHER = "Department of Health"
_RSS_URL = "https://www.health-ni.gov.uk/news/feed/health"
_NS_DC = "http://purl.org/dc/elements/1.1/"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Module-level helpers (no self dependency — safe to call from anywhere)
# ---------------------------------------------------------------------------

def _curl_get(url: str, *, timeout: int = 30, retries: int = 3):
    """Fetch URL via curl with TLS-max 1.3 and exponential-backoff retries.

    Returns decoded str or None on total failure.
    """
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", f"User-Agent: {_UA}",
        "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "-H", "Accept-Language: en-GB,en;q=0.9",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            raw = result.stdout.decode("utf-8", errors="replace")
            if raw.strip():
                return raw
            if attempt < retries - 1:
                wait = 3 ** attempt
                print(f"[{_SITE_ID}] empty response for {url}, "
                      f"retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 3 ** attempt
                print(f"[{_SITE_ID}] curl error: {exc}, "
                      f"retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
            else:
                print(f"[{_SITE_ID}] curl failed after {retries} attempts: {exc}")
    return None


def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("No HTML parser available")


def _parse_date(raw: str) -> str:
    """Convert date string → 'YYYY-MM-DD'. Never crashes."""
    if not raw:
        return ""
    raw = raw.strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", raw)
    if m:
        return m.group(1)
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


def _extract_node_id(html: str):
    """Extract Drupal node ID from page HTML (e.g. /node/425389)."""
    m = re.search(r'/node/(\d+)', html)
    if m:
        return m.group(1)
    # Settings JSON fallback
    m = re.search(r'"currentPath"\s*:\s*"node(?:\\?/)(\d+)"', html)
    if m:
        return m.group(1)
    return None


def _parse_rss_page(page_num: int):
    """Fetch and parse one RSS feed page.

    Returns list of {title, url, description, pub_date_raw, creator} dicts.
    Empty list on failure or no items.
    """
    url = f"{_RSS_URL}?page={page_num}"
    raw = _curl_get(url, timeout=30)
    if not raw:
        return []

    try:
        root = ET.fromstring(raw)
    except Exception as exc:
        print(f"[{_SITE_ID}] RSS XML parse error page {page_num}: {exc}")
        return []

    items = []
    for item in root.findall(".//item"):
        try:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            description = (item.findtext("description") or "").strip()
            pub_date = (item.findtext("pubDate") or "").strip()
            creator = (item.findtext(f"{{{_NS_DC}}}creator") or _PUBLISHER).strip()

            if not link or not title:
                continue

            items.append({
                "title": title,
                "url": link,
                "description": description,
                "pub_date_raw": pub_date,
                "creator": creator,
            })
        except Exception as exc:
            print(f"[{_SITE_ID}] RSS item parse error: {exc}")
            continue

    return items


def _fetch_detail(url: str):
    """Fetch and parse a detail page.

    Returns (abstract_text, node_id). Either may be None on failure.
    """
    html = _curl_get(url, timeout=30)
    if not html:
        return None, None

    node_id = _extract_node_id(html)

    soup = None
    try:
        soup = _make_soup(html)
    except Exception as exc:
        print(f"[{_SITE_ID}] soup parse error for {url}: {exc}")
        return None, node_id

    # Find main article element
    article = soup.find("article")
    if not article:
        article = soup.find("main") or soup.find("div", {"id": "main-content"})

    if not article:
        return None, node_id

    # Remove non-content tags
    for tag in article.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()

    text = article.get_text(separator=" ", strip=True)

    # Strip leading "Title … Date published: DD Month YYYY" prefix
    text = re.sub(
        r'^.{0,300}?Date published:\s*\d+\s+\w+\s+\d{4}\s*',
        "",
        text,
        flags=re.DOTALL,
    ).strip()

    # Normalise whitespace and non-breaking spaces
    text = re.sub(r'\s+', ' ', text).strip()
    text = text.replace('\xa0', ' ')

    return (text if text else None), node_id


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class HealthNiGovUkNewsCrawler(BaseCrawler):
    site_id = "health-ni-gov-uk-news"
    site_name = "Custom: health-ni-gov-uk-news"
    base_url = "https://www.health-ni.gov.uk"

    def crawl(self, limit=None):
        """Crawl news articles via paginated RSS feed.

        Walks pages until (a) saved >= limit, (b) a page returns 0 new URLs,
        or (c) the 200-page safety cap / 25-minute budget is hit.
        """
        saved = 0
        seen_urls = set()
        page = 0
        limit_or_inf = limit if limit is not None else "∞"
        start_time = time.time()
        MAX_PAGES = 200
        BUDGET_SECS = 25 * 60

        try:
            while page < MAX_PAGES:
                elapsed = time.time() - start_time
                if elapsed > BUDGET_SECS:
                    print(f"[{self.site_id}] Time budget ({BUDGET_SECS}s) exceeded, stopping.")
                    break

                if limit is not None and saved >= limit:
                    break

                if page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

                items = _parse_rss_page(page)

                if not items:
                    print(f"[{self.site_id}] page {page}: no items returned, stopping.")
                    break

                new_items = [i for i in items if i["url"] not in seen_urls]
                if not new_items:
                    print(f"[{self.site_id}] page {page}: all items already seen, stopping.")
                    break

                for item in new_items:
                    if limit is not None and saved >= limit:
                        break

                    url = item["url"]
                    seen_urls.add(url)

                    try:
                        time.sleep(self._delay)
                        abstract, node_id = _fetch_detail(url)

                        # Fall back to RSS description if detail page yields nothing useful
                        if not abstract or len(abstract) < 50:
                            abstract = item["description"]

                        if not abstract or len(abstract) < 50:
                            print(
                                f"[{self.site_id}] skip {url}: "
                                f"abstract too short ({len(abstract or '')} chars)"
                            )
                            continue

                        published_date = _parse_date(item["pub_date_raw"])
                        slug = url.rstrip("/").split("/")[-1]
                        external_id = node_id or slug
                        post_number = node_id  # numeric Drupal node ID string

                        paper = {
                            "id": None,
                            "site_id": self.site_id,
                            "external_id": external_id,
                            "post_number": post_number,
                            "title": item["title"],
                            "abstract": abstract,
                            "published_date": published_date,
                            "listed_date": published_date,
                            "url": url,
                            "pdf_url": None,
                            "authors": "",
                            "publisher": item["creator"] or _PUBLISHER,
                            "department": "",
                            "journal": "",
                            "keywords": "",
                            "category": "News",
                            "doi": "",
                            "original_filename": None,
                            "metadata": json.dumps({
                                "posted_date": item["pub_date_raw"],
                                "node_id": node_id,
                                "slug": slug,
                                "rss_description": item["description"],
                                "creator": item["creator"],
                            }, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1

                    except Exception as exc:
                        print(f"[{self.site_id}] item {url} failed: {exc}")
                        continue

                if page == MAX_PAGES - 1:
                    print(f"[{self.site_id}] Safety cap of {MAX_PAGES} pages reached.")

                page += 1

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted after {saved} saved.")
            raise

        print(f"[{self.site_id}] Done: {saved} items saved.")
        return saved
