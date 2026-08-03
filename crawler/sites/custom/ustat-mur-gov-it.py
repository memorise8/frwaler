# -*- coding: utf-8 -*-
"""Crawler for ustat.mur.gov.it — Italian MUR higher-education statistics portal.

Covers the /documenti/ listing and the /documenti/archivio-documenti/ legacy
PDF catalogue. Each modern item has a rich detail page; archive items are
PDF-only.
"""

import json
import os
import re
import subprocess
import sys
import time
import uuid
from urllib.parse import urljoin, unquote

sys.path.insert(0, '.')
from crawler.base_crawler import BaseCrawler  # noqa: E402

SITE_ID = 'ustat-mur-gov-it'

ITALIAN_MONTHS = {
    'gennaio': '01', 'febbraio': '02', 'marzo': '03', 'aprile': '04',
    'maggio': '05', 'giugno': '06', 'luglio': '07', 'agosto': '08',
    'settembre': '09', 'ottobre': '10', 'novembre': '11', 'dicembre': '12',
}


def _parse_italian_date(text):
    """'09 febbraio 2026' → '2026-02-09', or None on failure."""
    if not text:
        return None
    m = re.match(r'(\d{1,2})\s+(\w+)\s+(\d{4})', text.strip().lower())
    if not m:
        return None
    day, month_it, year = m.group(1), m.group(2), m.group(3)
    month = ITALIAN_MONTHS.get(month_it)
    if not month:
        return None
    return f"{year}-{month}-{day.zfill(2)}"


def _curl_get(url, retries=3):
    """Fetch URL via curl with TLS options. Returns (bytes, None) or (None, err_str)."""
    err = 'unknown'
    for attempt in range(retries):
        try:
            result = subprocess.run(
                ['curl', '--tls-max', '1.3', '-sk', '-L', '--max-time', '30', url],
                capture_output=True,
                timeout=45,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout, None
            err = f"curl exit {result.returncode}"
        except Exception as exc:
            err = str(exc)
        if attempt < retries - 1:
            wait = 3 ** attempt  # 1s, 3s, 9s
            print(f"[{SITE_ID}] retry {attempt + 1}/{retries} for {url} ({err}), sleep {wait}s")
            time.sleep(wait)
    return None, err


def _parse_html(raw_bytes):
    """Parse HTML bytes with fallback parser chain. Returns BeautifulSoup or None."""
    try:
        text = raw_bytes.decode('utf-8', errors='replace')
    except Exception:
        text = raw_bytes.decode('latin-1', errors='replace')

    for parser in ('html5lib', 'lxml', 'html.parser'):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(text, parser)
        except Exception:
            continue
    return None


def _text(el):
    """Get collapsed plain text from a BeautifulSoup element."""
    if el is None:
        return ''
    return re.sub(r'\s+', ' ', el.get_text(' ', strip=True)).strip()


# ---------------------------------------------------------------------------


class UstatMurGovItCrawler(BaseCrawler):
    site_id = 'ustat-mur-gov-it'
    site_name = 'Custom: ustat-mur-gov-it'
    base_url = 'https://ustat.mur.gov.it'

    def crawl(self, limit=None):
        start_time = time.time()
        MAX_WALL_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

        saved = 0
        seen_urls = set()
        limit_or_inf = limit if limit is not None else float('inf')

        # ------------------------------------------------------------------
        # Phase 1: collect all list items
        # ------------------------------------------------------------------
        listing_pages = [
            'https://ustat.mur.gov.it/documenti/',
            'https://ustat.mur.gov.it/documenti/archivio-documenti/',
        ]

        # Each entry: (date_str, page_url, title, snippet, is_pdf_only, direct_pdf_url)
        all_items = []

        for list_url in listing_pages:
            raw, err = _curl_get(list_url)
            if not raw:
                print(f"[{SITE_ID}] failed to fetch listing {list_url}: {err}")
                continue
            soup = _parse_html(raw)
            if soup is None:
                print(f"[{SITE_ID}] failed to parse listing {list_url}")
                continue

            # Modern .news items (each links to a detail page)
            for news_div in soup.find_all('div', class_='news'):
                try:
                    date_span = news_div.find('span')
                    date_str = _text(date_span) if date_span else ''

                    a_tag = news_div.find('a')
                    if not a_tag:
                        continue
                    href = a_tag.get('href', '')
                    title = _text(a_tag)
                    if not href or not title:
                        continue

                    full_url = urljoin(self.base_url, href)

                    snippet = ''
                    for child_div in news_div.find_all('div'):
                        t = _text(child_div)
                        if t:
                            snippet = t
                            break

                    all_items.append((date_str, full_url, title, snippet, False, None))
                except Exception as exc:
                    print(f"[{SITE_ID}] list item parse error: {exc}")
                    continue

            # Archive page: direct PDF links in <li><a href="*.pdf">
            if 'archivio' in list_url:
                for li in soup.find_all('li'):
                    try:
                        a = li.find('a')
                        if not a:
                            continue
                        href = a.get('href', '')
                        if not re.search(r'\.pdf', href, re.I):
                            continue
                        title = _text(a)
                        full_url = urljoin(self.base_url, href)
                        all_items.append(('', full_url, title, '', True, full_url))
                    except Exception as exc:
                        print(f"[{SITE_ID}] archive item parse error: {exc}")
                        continue

        print(f"[{SITE_ID}] discovered {len(all_items)} items across listing pages")

        # ------------------------------------------------------------------
        # Phase 2: process items in listing order (newest first)
        # ------------------------------------------------------------------
        for idx, (date_str, item_url, title, snippet, is_pdf_only, direct_pdf_url) in enumerate(all_items):
            if saved >= limit_or_inf:
                break

            if time.time() - start_time > MAX_WALL_SECS:
                print(f"[{SITE_ID}] 25-minute wall-clock budget reached — stopping cleanly")
                break

            if item_url in seen_urls:
                continue
            seen_urls.add(item_url)

            if idx > 0 and idx % 10 == 0:
                print(f"[{SITE_ID}] page/item {idx}: saved {saved}/{limit_or_inf}")

            try:
                if is_pdf_only:
                    # Archive PDF — no detail page; abstract = title (likely short → will be skipped)
                    abstract = title
                    pdf_url = direct_pdf_url
                    detail_url = None
                    published_date = _parse_italian_date(date_str)
                    original_filename = unquote(item_url.rstrip('/').split('/')[-1])
                else:
                    time.sleep(self._delay)
                    raw, err = _curl_get(item_url)
                    if not raw:
                        print(f"[{SITE_ID}] item {idx} fetch failed: {err}")
                        continue

                    soup = _parse_html(raw)
                    if soup is None:
                        print(f"[{SITE_ID}] item {idx} parse failed")
                        continue

                    # Full abstract from the content div
                    content_div = soup.find(id='content')
                    if content_div:
                        abstract = _text(content_div)
                    else:
                        abstract = snippet

                    if not abstract or len(abstract) < 50:
                        abstract = snippet

                    # First PDF link that isn't the MUR privacy notice
                    pdf_url = None
                    original_filename = None
                    for a in soup.find_all('a', href=True):
                        href = a['href']
                        if re.search(r'\.pdf', href, re.I) and 'mur.gov.it/sites' not in href:
                            pdf_url = urljoin(self.base_url, href)
                            raw_fn = pdf_url.rstrip('/').split('/')[-1].split('?')[0]
                            original_filename = unquote(raw_fn)
                            break

                    detail_url = item_url
                    published_date = _parse_italian_date(date_str)

                # Skip items whose abstract is too short to be useful
                if not abstract or len(abstract) < 50:
                    print(f"[{SITE_ID}] skip {item_url}: abstract too short ({len(abstract or '')} chars)")
                    continue

                # Derive slug-based IDs
                slug = item_url.rstrip('/').split('/')[-1]
                if not slug:
                    slug = str(uuid.uuid4())
                external_id = slug

                # post_number: date as YYYYMMDD for incremental-crawl ordering
                if published_date:
                    post_number = published_date.replace('-', '')
                else:
                    post_number = None

                metadata = {
                    'slug': slug,
                    'list_snippet': snippet,
                }
                if is_pdf_only:
                    metadata['pdf_only'] = True

                paper = {
                    'site_id': self.site_id,
                    'external_id': external_id,
                    'post_number': post_number,
                    'title': title,
                    'abstract': abstract,
                    'published_date': published_date,
                    'posted_date': published_date,
                    'publisher': "USTAT - Ministero dell'Università e della Ricerca",
                    'url': detail_url or item_url,
                    'pdf_url': pdf_url,
                    'original_filename': original_filename,
                    'category': 'documenti',
                    'metadata': json.dumps(metadata, ensure_ascii=False),
                }

                self._save_paper(paper)
                saved += 1
                print(f"[{SITE_ID}] saved {saved}: {title[:70]}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{SITE_ID}] item {idx} failed: {exc}")
                continue

        print(f"[{SITE_ID}] done — total saved: {saved}")
        return saved
