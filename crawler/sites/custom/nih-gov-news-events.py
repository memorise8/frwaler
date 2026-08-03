# -*- coding: utf-8 -*-
"""NIH News Releases crawler — https://www.nih.gov/news-events/news-releases"""

import json
import re
import subprocess
import time
from datetime import datetime

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from crawler.base_crawler import BaseCrawler

_MONTH_MAP = {
    'january': '01', 'february': '02', 'march': '03', 'april': '04',
    'may': '05', 'june': '06', 'july': '07', 'august': '08',
    'september': '09', 'october': '10', 'november': '11', 'december': '12',
}

_LIST_URL = "https://www.nih.gov/news-events/news-releases"
_BASE = "https://www.nih.gov"


def _bs(raw: str):
    """Parse HTML with best available parser; returns BeautifulSoup or None."""
    for parser in ('html5lib', 'lxml', 'html.parser'):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _curl_get(url: str, retries: int = 3) -> str | None:
    """Fetch URL via curl with retries and exponential backoff."""
    cmd = [
        'curl', '-sk', '--tls-max', '1.3', '--max-time', '30',
        '-A', BaseCrawler.USER_AGENT,
        '-H', 'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        '-H', 'Accept-Language: en-US,en;q=0.9',
        '-L', url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            raw = result.stdout.decode('utf-8', errors='replace')
            if raw.strip():
                return raw
        except Exception as exc:
            print(f'[nih-gov-news-events] curl error attempt {attempt+1}/{retries}: {exc}')
        if attempt < retries - 1:
            wait = (attempt + 1) ** 2  # 1s, 4s, 9s
            time.sleep(wait)
    return None


def _parse_date_text(text: str) -> str:
    """Convert 'May 6, 2026' -> '2026-05-06'."""
    m = re.search(
        r'(January|February|March|April|May|June|July|August|September|October|November|December)'
        r'\s+(\d{1,2}),\s+(\d{4})',
        text, re.IGNORECASE
    )
    if not m:
        return ''
    month = _MONTH_MAP.get(m.group(1).lower(), '01')
    day = m.group(2).zfill(2)
    year = m.group(3)
    return f'{year}-{month}-{day}'


def _parse_date_iso(text: str) -> str:
    """Extract YYYY-MM-DD from ISO datetime like '2026-05-06T12:12:07-04:00'."""
    m = re.search(r'(\d{4}-\d{2}-\d{2})', text)
    return m.group(1) if m else ''


def _strip_tags(html: str) -> str:
    """Strip HTML tags, decode common entities, normalize whitespace."""
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&[a-zA-Z]+;', '', text)
    return re.sub(r'\s+', ' ', text).strip()


class NihGovNewsEventsCrawler(BaseCrawler):
    """Crawler for NIH News Releases."""

    site_id = "nih-gov-news-events"
    site_name = "Custom: nih-gov-news-events"
    base_url = "https://www.nih.gov"

    def _fetch_list_page(self, page: int) -> list[dict]:
        """Fetch one listing page; return list of {url, title, date} dicts."""
        url = f"{_LIST_URL}?page={page}"
        raw = _curl_get(url)
        if not raw:
            return []

        items = []
        # Parse teaser <li> items
        teaser_blocks = re.findall(
            r'<li[^>]*class="[^"]*teaser[^"]*"[^>]*>(.*?)</li>',
            raw, re.DOTALL
        )

        if not teaser_blocks:
            # Fallback: find all news-release links in main content
            main_m = re.search(r'id="main-content"(.*)', raw, re.DOTALL)
            main_chunk = main_m.group(1) if main_m else raw
            links = re.findall(
                r'href="(/news-events/news-releases/[^"?#]+)"',
                main_chunk
            )
            for href in links:
                items.append({'url': _BASE + href, 'title': '', 'date': ''})
            return items

        for block in teaser_blocks:
            link_m = re.search(r'href="(/news-events/news-releases/[^"?#]+)"', block)
            if not link_m:
                continue
            href = link_m.group(1)
            article_url = _BASE + href

            title_m = re.search(r'<h\d[^>]*>(.*?)</h\d>', block, re.DOTALL)
            title = _strip_tags(title_m.group(1)) if title_m else ''

            date = _parse_date_text(block)

            items.append({'url': article_url, 'title': title, 'date': date})

        return items

    def _fetch_detail(self, url: str) -> dict | None:
        """Fetch detail page; return parsed record or None."""
        raw = _curl_get(url)
        if not raw:
            return None

        # Node ID from shortlink
        node_m = re.search(r'<link rel="shortlink" href="/node/(\d+)"', raw)
        node_id = node_m.group(1) if node_m else ''

        # Title from og:title
        og_title_m = re.search(r'<meta property="og:title" content="([^"]+)"', raw)
        title = og_title_m.group(1).strip() if og_title_m else ''

        # Date from og:updated_time (og:article:published_time rarely present)
        date = ''
        for pat in [
            r'<meta property="article:published_time" content="([^"]+)"',
            r'<meta property="og:updated_time" content="([^"]+)"',
        ]:
            dm = re.search(pat, raw)
            if dm:
                date = _parse_date_iso(dm.group(1))
                if date:
                    break

        # Abstract: paragraphs from the main content area
        main_m = re.search(r'id="main-content"(.*)', raw, re.DOTALL)
        main_chunk = main_m.group(1) if main_m else raw

        # Use BeautifulSoup if available for robust parsing, else regex
        abstract_parts = []
        try:
            soup = _bs(main_chunk)
            if soup:
                for tag in soup.find_all(['p', 'h2', 'h3']):
                    text = tag.get_text(separator=' ', strip=True)
                    text = re.sub(r'\s+', ' ', text).strip()
                    if len(text) > 40:
                        abstract_parts.append(text)
        except Exception:
            pass

        if not abstract_parts:
            # Regex fallback
            for m in re.finditer(r'<p[^>]*>(.*?)</p>', main_chunk, re.DOTALL):
                text = _strip_tags(m.group(1))
                if len(text) > 40:
                    abstract_parts.append(text)

        # Filter out common boilerplate (gov banner text)
        boilerplate_triggers = [
            'official website', 'secure .gov', 'lock', 'https://',
            'share sensitive information', 'here\'s how you know',
        ]
        filtered = []
        for part in abstract_parts:
            lower = part.lower()
            if any(t in lower for t in boilerplate_triggers):
                continue
            filtered.append(part)
        abstract_parts = filtered

        abstract = '\n\n'.join(abstract_parts)

        # Keywords from meta keywords tag
        kw_m = re.search(r'<meta name="keywords" content="([^"]+)"', raw)
        keywords_raw = kw_m.group(1) if kw_m else ''
        keywords = [k.strip() for k in keywords_raw.split(',') if k.strip()] if keywords_raw else []

        return {
            'node_id': node_id,
            'title': title,
            'date': date,
            'abstract': abstract,
            'keywords': keywords,
        }

    def crawl(self, limit=None) -> int:
        """Crawl NIH news releases listing pages and fetch detail pages."""
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute wall-clock budget
        MAX_PAGES = 200

        limit_display = str(limit) if limit is not None else 'inf'

        page = 0
        while True:
            # Wall-clock budget check
            if time.time() - start_time > MAX_SECONDS:
                print(f'[nih-gov-news-events] Approaching time budget, stopping cleanly.')
                break

            if limit is not None and saved >= limit:
                break

            if page >= MAX_PAGES:
                print(f'[nih-gov-news-events] Safety cap of {MAX_PAGES} pages reached, stopping.')
                break

            if page % 10 == 0 and page > 0:
                print(f'[nih-gov-news-events] page {page}: saved {saved}/{limit_display}')

            try:
                items = self._fetch_list_page(page)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f'[nih-gov-news-events] List page {page} error: {exc}')
                items = []

            if not items:
                print(f'[nih-gov-news-events] Page {page} returned 0 items — end of pagination.')
                break

            # Deduplicate across pages
            new_items = []
            for item in items:
                if item['url'] not in seen_urls:
                    seen_urls.add(item['url'])
                    new_items.append(item)

            if not new_items:
                print(f'[nih-gov-news-events] Page {page}: all items already seen — end of pagination.')
                break

            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                article_url = item['url']
                list_date = item['date']
                list_title = item['title']

                # Rate limit between detail fetches
                time.sleep(self._delay)

                try:
                    detail = self._fetch_detail(article_url)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f'[nih-gov-news-events] item {article_url} failed: {exc}')
                    continue

                if not detail:
                    print(f'[nih-gov-news-events] item {article_url} failed: no detail returned')
                    continue

                title = detail['title'] or list_title
                if not title:
                    print(f'[nih-gov-news-events] item {article_url} failed: no title')
                    continue

                abstract = detail['abstract']
                if len(abstract) < 50:
                    print(f'[nih-gov-news-events] item {article_url} short abstract ({len(abstract)} chars), skipping')
                    continue

                date = detail['date'] or list_date
                node_id = detail['node_id']
                external_id = node_id or re.sub(r'.*/news-releases/', '', article_url)

                keywords = detail['keywords']

                paper = {
                    'id': None,
                    'site_id': self.site_id,
                    'external_id': external_id,
                    'title': title,
                    'authors': json.dumps([], ensure_ascii=False),
                    'abstract': abstract,
                    'category': 'News Release',
                    'keywords': json.dumps(keywords, ensure_ascii=False),
                    'published_date': date,
                    'url': article_url,
                    'pdf_url': '',
                    'doi': '',
                    'department': 'National Institutes of Health',
                    'metadata': json.dumps({
                        'node_id': node_id,
                        'source': 'NIH News Releases',
                    }, ensure_ascii=False),
                }

                try:
                    self._save_paper(paper)
                    saved += 1
                    print(f'[nih-gov-news-events] saved {saved}/{limit_display}: {title[:70]}')
                except Exception as exc:
                    print(f'[nih-gov-news-events] save failed for {article_url}: {exc}')
                    continue

            page += 1

        print(f'[nih-gov-news-events] Done. Total saved: {saved}')
        return saved
