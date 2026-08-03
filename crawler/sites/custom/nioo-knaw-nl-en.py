# -*- coding: utf-8 -*-
"""NIOO-KNAW (Netherlands Institute of Ecology) press release crawler."""

import json
import re
import subprocess
import time

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from crawler.base_crawler import BaseCrawler

_SITE_ID   = "nioo-knaw-nl-en"
_BASE_URL  = "https://nioo.knaw.nl"
_LIST_URL  = "https://nioo.knaw.nl/en/pressreleases"
_PAGE_SIZE = 12
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_PUBLISHER = "Netherlands Institute of Ecology (NIOO-KNAW)"


def _curl_get(url: str, retries: int = 3) -> str | None:
    """Fetch URL via curl with retries and exponential backoff."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
        "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            raw = result.stdout
            if raw:
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    return raw.decode("utf-8", errors="replace")
            if attempt < retries - 1:
                wait = (attempt + 1) ** 2
                print(f"[{_SITE_ID}] Empty response for {url}, retry in {wait}s...")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = (attempt + 1) ** 2
                print(f"[{_SITE_ID}] curl error ({url}): {exc}, retry in {wait}s...")
                time.sleep(wait)
            else:
                print(f"[{_SITE_ID}] curl failed after {retries} attempts: {exc}")
    return None


def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib -> lxml -> html.parser."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _strip_tags(html_frag: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    text = re.sub(r"<[^>]+>", " ", html_frag)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z0-9#]+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_listing_page(html: str):
    """Return list of (url, title, listed_date) from a press-release listing page."""
    items = []
    # Split on article card boundaries
    blocks = re.split(r"full-click node node--type-news-item", html)
    for block in blocks[1:]:  # skip text before first article
        link_m  = re.search(r'href="(/en/news/[^"]+)"[^>]*full-click__trigger', block, re.DOTALL)
        if not link_m:
            # try reversed order: class before href (shouldn't happen but be safe)
            link_m = re.search(r'full-click__trigger[^>]*href="(/en/news/[^"]+)"', block, re.DOTALL)
        if not link_m:
            continue
        path = link_m.group(1)
        url  = _BASE_URL + path

        title_m = re.search(r'class="text">([^<]+)</span>', block)
        title   = title_m.group(1).strip() if title_m else ""

        date_m  = re.search(r'datetime="(\d{4}-\d{2}-\d{2})"', block)
        listed_date = date_m.group(1) if date_m else ""

        items.append((url, title, listed_date))
    return items


def _parse_detail_page(html: str):
    """Extract node_id, published_date, abstract from a detail page.

    Returns dict with keys: node_id, published_date, abstract.
    """
    result = {"node_id": None, "published_date": "", "abstract": ""}

    # Node ID from Drupal settings JSON
    settings_m = re.search(
        r'data-drupal-selector="drupal-settings-json">(\{.*?\})</script>',
        html, re.DOTALL
    )
    if settings_m:
        try:
            d = json.loads(settings_m.group(1))
            current_path = d.get("path", {}).get("currentPath", "")
            if current_path.startswith("node/"):
                result["node_id"] = current_path.split("/")[-1]
        except Exception:
            pass

    # Published date (first datetime in page is the article date)
    date_m = re.search(r'datetime="(\d{4}-\d{2}-\d{2})"', html)
    if date_m:
        result["published_date"] = date_m.group(1)

    # Abstract: gather all text-formatted div content, skip boilerplate tail
    # The last chunk is always the "subscribe to updates" blurb — skip it
    chunks = re.findall(
        r'class="text-formatted[^"]*">(.*?)</div>',
        html, re.DOTALL
    )
    boilerplate_phrases = (
        "you can follow nioo",
        "log in or set up a new account",
        "subscribe to updates",
        "stay informed about",
        "click ‘follow’",
    )
    good_chunks = []
    for chunk in chunks:
        text = _strip_tags(chunk)
        if not text:
            continue
        lower = text.lower()
        if any(phrase in lower for phrase in boilerplate_phrases):
            continue
        good_chunks.append(text)

    result["abstract"] = "\n\n".join(good_chunks)
    return result


class NiooKnawNlEnCrawler(BaseCrawler):
    """Press release crawler for Netherlands Institute of Ecology (NIOO-KNAW)."""

    site_id   = _SITE_ID
    site_name = "Custom: nioo-knaw-nl-en"
    base_url  = _BASE_URL

    def crawl(self, limit=None):
        """Crawl NIOO-KNAW press releases.

        Paginates through listing pages, fetches each detail page for the
        full abstract and node ID, then saves via self._save_paper().
        Returns the total number of records saved.
        """
        saved     = 0
        seen_urls = set()
        limit_inf = limit is None
        start_time = time.time()
        max_seconds = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute wall-clock budget

        for page_num in range(_MAX_PAGES):
            # Wall-clock guard
            if time.time() - start_time > max_seconds:
                print(f"[{_SITE_ID}] 25-minute budget reached at page {page_num}. Exiting.")
                break

            if not limit_inf and saved >= limit:
                break

            url = f"{_LIST_URL}?page={page_num}"
            html = _curl_get(url)
            if not html:
                print(f"[{_SITE_ID}] Failed to fetch listing page {page_num}. Stopping.")
                break

            items = _parse_listing_page(html)
            if not items:
                print(f"[{_SITE_ID}] No items on page {page_num}. Pagination complete.")
                break

            if page_num % 10 == 0:
                limit_label = str(limit) if not limit_inf else "∞"
                print(f"[{_SITE_ID}] page {page_num}: saved {saved}/{limit_label}")

            # Check for page-loop (paginator cycling back to page 0)
            page_urls = {u for u, _, _ in items}
            if page_urls.issubset(seen_urls):
                print(f"[{_SITE_ID}] All URLs on page {page_num} already seen. Stopping.")
                break

            new_items = [(u, t, d) for u, t, d in items if u not in seen_urls]
            for u in page_urls:
                seen_urls.add(u)

            for item_url, listing_title, listed_date in new_items:
                if not limit_inf and saved >= limit:
                    break

                # Per-item isolation
                try:
                    time.sleep(self._delay)

                    detail_html = _curl_get(item_url)
                    if not detail_html:
                        print(f"[{_SITE_ID}] Could not fetch detail: {item_url}")
                        continue

                    detail = _parse_detail_page(detail_html)
                    node_id      = detail["node_id"]
                    published    = detail["published_date"] or listed_date
                    abstract     = detail["abstract"]

                    if len(abstract) < 50:
                        print(f"[{_SITE_ID}] Abstract too short ({len(abstract)} chars), skipping: {item_url}")
                        continue

                    # Use node_id as post_number (numeric string)
                    post_number = node_id  # e.g. "1519"
                    external_id = node_id or item_url.split("/en/news/")[-1]

                    # URL slug for filename / original_filename fallback
                    slug = item_url.rstrip("/").split("/")[-1]

                    paper = {
                        "site_id":           self.site_id,
                        "external_id":       external_id,
                        "post_number":       post_number,
                        "title":             listing_title,
                        "abstract":          abstract,
                        "published_date":    published,
                        "listed_date":       listed_date,
                        "url":               item_url,
                        "pdf_url":           None,
                        "authors":           None,
                        "publisher":         _PUBLISHER,
                        "department":        None,
                        "journal":           None,
                        "keywords":          None,
                        "category":          "Press release",
                        "doi":               None,
                        "original_filename": None,
                        "metadata": json.dumps({
                            "posted_date": listed_date,
                            "node_id":     node_id,
                            "slug":        slug,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    limit_label = str(limit) if not limit_inf else "∞"
                    print(f"[{_SITE_ID}] Saved {saved}/{limit_label}: {listing_title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] Item failed ({item_url}): {exc}")
                    continue

            if page_num == _MAX_PAGES - 1:
                print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached.")

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
