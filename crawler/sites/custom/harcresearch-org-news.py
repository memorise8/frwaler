# -*- coding: utf-8 -*-
"""Crawler for HARC Research (harcresearch.org) news articles.

Strategy: The listing page loads all ~238 items at once via Isotope.js
(no server-side pagination). We parse titles/dates/categories from the
listing, then fetch each detail page for the article body (abstract).
"""

import json
import os
import re
import subprocess
import sys
import time

# Absolute import — spec_from_file_location has no package context
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from crawler.base_crawler import BaseCrawler

_SITE_ID = "harcresearch-org-news"
_LIST_URL = "https://harcresearch.org/news/"
_PUBLISHER = "Houston Advanced Research Center"
_MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_BS4_PARSERS = ["html5lib", "lxml", "html.parser"]


# ---------------------------------------------------------------------------
# HTTP / parsing helpers
# ---------------------------------------------------------------------------

def _curl_get(url: str, retries: int = 3) -> str:
    """Fetch URL via curl with exponential-backoff retries."""
    delays = [1, 3, 9]
    for attempt in range(retries):
        try:
            result = subprocess.run(
                [
                    "curl", "--tls-max", "1.3", "-sk", "-L",
                    "--max-time", "30",
                    "-A", (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    url,
                ],
                capture_output=True,
                timeout=35,
            )
            if result.returncode == 0 and result.stdout:
                try:
                    return result.stdout.decode("utf-8", errors="replace")
                except Exception:
                    return result.stdout.decode("latin-1", errors="replace")
        except Exception as exc:
            print(f"[{_SITE_ID}] curl error attempt {attempt + 1}/{retries}: {exc}")
        if attempt < retries - 1:
            time.sleep(delays[attempt])
    return ""


def _make_soup(html: str):
    """Build BeautifulSoup with parser fallback chain."""
    from bs4 import BeautifulSoup
    for parser in _BS4_PARSERS:
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _parse_date(date_str: str):
    """Convert MM.DD.YYYY → YYYY-MM-DD. Returns None on failure."""
    if not date_str:
        return None
    s = date_str.strip()
    m = re.match(r'^(\d{2})\.(\d{2})\.(\d{4})$', s)
    if m:
        month, day, year = m.groups()
        return f"{year}-{month}-{day}"
    m = re.match(r'^\d{4}-\d{2}-\d{2}', s)
    if m:
        return s[:10]
    return s or None


def _parse_listing(html: str) -> list:
    """Return list of {url, title, category, date_raw} dicts from listing HTML."""
    items = []
    pattern = re.compile(
        r'<div\s+class="item[^"]*">\s*'
        r'<a\s+href="([^"]+)">'
        r'.*?'
        r'<span\s+class="h3">(.*?)</span>'
        r'.*?'
        r'<span\s+class="h6">(.*?)</span>'
        r'.*?'
        r'<span\s+class="h5">(.*?)</span>',
        re.DOTALL,
    )
    for m in pattern.finditer(html):
        url = m.group(1).strip()
        title = re.sub(r'<[^>]+>', '', m.group(2)).strip()
        category = re.sub(r'<[^>]+>', '', m.group(3)).strip()
        date_raw = m.group(4).strip()
        if url and title:
            items.append({
                'url': url,
                'title': title,
                'category': category,
                'date_raw': date_raw,
            })
    return items


def _extract_abstract(html: str) -> str:
    """Extract article body text from a HARC detail page.

    Tries BeautifulSoup on content-block first, falls back to all <p>
    tags after stripping nav/header/footer/script/style.
    """
    soup = _make_soup(html)
    if soup:
        for tag in soup.find_all(['nav', 'header', 'footer', 'script', 'style', 'aside']):
            tag.decompose()
        # Try the HARC theme's main article container
        container = soup.find(class_='content-block')
        if container:
            texts = [
                p.get_text(separator=' ', strip=True)
                for p in container.find_all('p')
                if len(p.get_text(strip=True)) > 50
            ]
            if texts:
                return ' '.join(texts)
        # Fall back to all substantive paragraphs in the body
        body = soup.body or soup
        texts = [
            p.get_text(separator=' ', strip=True)
            for p in body.find_all('p')
            if len(p.get_text(strip=True)) > 50
        ]
        if texts:
            return ' '.join(texts)

    # Regex fallback for malformed pages
    html_clean = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html_clean = re.sub(r'<style[^>]*>.*?</style>', '', html_clean, flags=re.DOTALL | re.IGNORECASE)
    paras = re.findall(r'<p[^>]*>(.*?)</p>', html_clean, re.DOTALL | re.IGNORECASE)
    texts = []
    for p in paras:
        t = re.sub(r'<[^>]+>', '', p).strip()
        if len(t) > 50:
            texts.append(t)
    return ' '.join(texts)


def _find_pdf_url(html: str):
    """Return first PDF URL found in the page, or None."""
    matches = re.findall(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', html, re.IGNORECASE)
    return matches[0] if matches else None


def _slug_from_url(url: str) -> str:
    return url.rstrip('/').split('/')[-1]


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class HarcResearchOrgNewsCrawler(BaseCrawler):
    site_id = "harcresearch-org-news"
    site_name = "Custom: harcresearch-org-news"
    base_url = "https://harcresearch.org"

    def crawl(self, limit=None):
        start_time = time.time()
        saved = 0
        seen_urls: set = set()
        limit_display = limit if limit is not None else 'inf'

        # ------------------------------------------------------------------
        # Step 1: fetch the single listing page (all items load at once)
        # ------------------------------------------------------------------
        print(f"[{_SITE_ID}] Fetching listing page {_LIST_URL}")
        list_html = _curl_get(_LIST_URL)
        if not list_html:
            print(f"[{_SITE_ID}] Failed to fetch listing page, aborting.")
            return saved

        items = _parse_listing(list_html)
        total = len(items)
        print(f"[{_SITE_ID}] Parsed {total} items from listing page.")

        if total == 0:
            print(f"[{_SITE_ID}] No items found — check HTML structure.")
            return saved

        # ------------------------------------------------------------------
        # Step 2: iterate items, fetch detail pages, save
        # The listing has no server-side pages (Isotope JS client filtering).
        # We treat the item list itself as our "pages", logging every 10 items.
        # ------------------------------------------------------------------
        for idx, item in enumerate(items):
            if limit is not None and saved >= limit:
                break

            elapsed = time.time() - start_time
            if elapsed > _MAX_WALL_SECONDS:
                print(f"[{_SITE_ID}] Wall-clock budget exceeded ({elapsed:.0f}s), stopping.")
                break

            if idx > 0 and idx % 10 == 0:
                print(f"[{_SITE_ID}] page 1: saved {saved}/{limit_display}")

            url = item['url']
            if url in seen_urls:
                continue
            seen_urls.add(url)

            try:
                detail_html = _curl_get(url)
                if not detail_html:
                    print(f"[{_SITE_ID}] item {url}: fetch failed, skipping")
                    continue

                abstract = _extract_abstract(detail_html)
                if not abstract or len(abstract) < 100:
                    print(
                        f"[{_SITE_ID}] item {url}: abstract too short "
                        f"({len(abstract) if abstract else 0} chars), skipping"
                    )
                    continue

                slug = _slug_from_url(url)
                published_date = _parse_date(item['date_raw'])
                pdf_url = _find_pdf_url(detail_html)

                pdf_filename = None
                if pdf_url:
                    tail = pdf_url.rstrip('/').split('/')[-1].split('?')[0]
                    if tail and '.' in tail:
                        pdf_filename = tail

                self._save_paper({
                    'site_id': self.site_id,
                    'external_id': slug,
                    'post_number': slug,
                    'title': item['title'],
                    'abstract': abstract,
                    'published_date': published_date,
                    'listed_date': published_date,
                    'url': url,
                    'pdf_url': pdf_url,
                    'original_filename': pdf_filename,
                    'publisher': _PUBLISHER,
                    'category': item['category'],
                    'keywords': None,
                    'authors': None,
                    'doi': None,
                    'metadata': json.dumps(
                        {
                            'posted_date': item['date_raw'],
                            'category': item['category'],
                            'slug': slug,
                        },
                        ensure_ascii=False,
                    ),
                })
                saved += 1
                time.sleep(self._delay)

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] item {url} failed: {exc}")
                continue

        print(f"[{_SITE_ID}] Done. saved={saved}/{limit_display}")
        return saved
