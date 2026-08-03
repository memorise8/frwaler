# -*- coding: utf-8 -*-
"""
Crawler for Canada Energy Regulator (CER) Publications and Reports.

Starting URL:
    https://www.cer-rec.gc.ca/en/about/publications-reports/index.html

Structure:
    Level 0 – Index page: WET-datatable listing publication category pages
    Level 1 – Category pages: links to individual publication HTML pages
    Level 2 – Publication pages: dcterms meta tags + body paragraphs for abstract
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Optional

# Absolute import — spec_from_file_location has no package context
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
from crawler.base_crawler import BaseCrawler

_SITE_ID = "cer-rec-gc-ca-en"
_BASE_URL = "https://www.cer-rec.gc.ca"
_INDEX_URL = f"{_BASE_URL}/en/about/publications-reports/index.html"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_DELAY = 1.0
_MIN_ABSTRACT_CHARS = 100
_RETRY_DELAYS = (1, 3, 9)


def _clean(text: str) -> str:
    return re.sub(r'\s+', ' ', text).strip()


def _parse_html(raw: str):
    """Parse HTML with html5lib → lxml → html.parser fallback chain."""
    from bs4 import BeautifulSoup
    for parser in ('html5lib', 'lxml', 'html.parser'):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    from bs4 import BeautifulSoup
    return BeautifulSoup('', 'html.parser')


def _curl(url: str, retries: int = 3) -> Optional[str]:
    """Fetch URL via curl with TLS 1.3 support. Returns decoded text or None."""
    for attempt in range(retries):
        try:
            result = subprocess.run(
                ['curl', '--tls-max', '1.3', '-sk', '-L', '--max-time', '30', url],
                capture_output=True,
                timeout=35,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout.decode('utf-8', errors='replace')
        except Exception as exc:
            print(f"[{_SITE_ID}] curl attempt {attempt+1}/{retries} failed for {url}: {exc}")
        if attempt < retries - 1:
            wait = _RETRY_DELAYS[attempt]
            print(f"[{_SITE_ID}] retrying in {wait}s…")
            time.sleep(wait)
    return None


def _to_abs(href: str) -> Optional[str]:
    """Convert href to absolute cer-rec.gc.ca URL. Returns None if external or invalid."""
    if not href:
        return None
    href = href.strip().split('#')[0]
    if not href:
        return None
    if href.startswith('http'):
        return href if 'cer-rec.gc.ca' in href else None
    if href.startswith('/'):
        return _BASE_URL + href
    return None


def _extract_meta(soup, name: str) -> str:
    m = soup.find('meta', attrs={'name': name})
    return _clean(m.get('content', '')) if m else ''


def _extract_abstract(soup) -> str:
    """Collect body paragraphs from <main> until we have ~600 chars."""
    main = soup.find('main') or soup.body
    if not main:
        return ''
    chunks: list[str] = []
    total = 0
    for p in main.find_all('p'):
        txt = _clean(p.get_text(separator=' '))
        if len(txt) < 20:
            continue
        lower = txt.lower()
        if any(kw in lower for kw in [
            'copyright', 'permission to reproduce', 'issn', 'isbn',
            'skip to', 'top of page',
        ]):
            continue
        chunks.append(txt)
        total += len(txt)
        if total >= 600:
            break
    return ' '.join(chunks)


def _get_category_urls(html: str) -> list[str]:
    """Extract unique category URLs from the index page <table> cells."""
    soup = _parse_html(html)
    table = soup.find('table')
    if not table:
        return []
    seen: set[str] = set()
    urls: list[str] = []
    for td in table.find_all('td'):
        a = td.find('a', href=True)
        if not a:
            continue
        url = _to_abs(a['href'])
        if url and url not in seen and '.pdf' not in url.lower():
            seen.add(url)
            urls.append(url)
    return urls


def _get_sub_links(soup, seen: set) -> list[str]:
    """Collect internal HTML links from the <main> content area."""
    main = soup.find('main') or soup.body
    if not main:
        return []
    links: list[str] = []
    for a in main.find_all('a', href=True):
        href = a['href']
        if '.pdf' in href.lower():
            continue
        url = _to_abs(href)
        if url and url not in seen and '/en/' in url:
            links.append(url)
    return links


def _extract_publication(soup, url: str) -> Optional[dict]:
    """
    Build a paper_dict from a detail page.
    Returns None if title is missing or abstract is under _MIN_ABSTRACT_CHARS.
    """
    # Title: dcterms.title → <h1>
    title = _extract_meta(soup, 'dcterms.title')
    title = re.sub(r'^CER\s*[-–—]\s*', '', title).strip()
    if not title:
        h1 = soup.find('h1')
        title = _clean(h1.get_text()) if h1 else ''
    if not title:
        return None

    # Date from dcterms.issued
    issued = soup.find('meta', attrs={'name': 'dcterms.issued', 'title': 'W3CDTF'})
    published_date = _clean(issued.get('content', '')) if issued else ''

    # Category / subject
    category = _extract_meta(soup, 'dcterms.subject').rstrip(';').strip()

    # Abstract from body paragraphs
    abstract = _extract_abstract(soup)
    if len(abstract) < _MIN_ABSTRACT_CHARS:
        return None

    # First PDF link in main content
    pdf_url: Optional[str] = None
    original_filename: Optional[str] = None
    main_tag = soup.find('main') or soup.body
    if main_tag:
        for a in main_tag.find_all('a', href=True):
            href = a['href']
            if '.pdf' in href.lower():
                pdf_url = _to_abs(href) or href
                if pdf_url:
                    original_filename = (
                        pdf_url.rstrip('/').split('/')[-1].split('?')[0] or None
                    )
                break

    # external_id = URL path (no native numeric ID on this site)
    external_id = urllib.parse.urlparse(url).path.rstrip('/')

    return {
        'site_id': _SITE_ID,
        'external_id': external_id,
        'post_number': external_id,   # no numeric ID — use slug
        'title': title,
        'abstract': abstract,
        'published_date': published_date,
        'listed_date': published_date,
        'url': url,
        'pdf_url': pdf_url,
        'original_filename': original_filename,
        'category': category or None,
        'publisher': 'Canada Energy Regulator',
        'authors': None,
        'keywords': category or None,
        'doi': None,
        'metadata': json.dumps({
            'posted_date': published_date,
            'originalFilename': original_filename,
            'source_url': url,
        }, ensure_ascii=False),
    }


class CerRecGcCaEnCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: cer-rec-gc-ca-en"
    base_url = _BASE_URL

    def crawl(self, limit=None):
        limit_val = limit if limit is not None else float('inf')
        limit_str = str(limit) if limit is not None else 'inf'
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        page_num = 0

        # ── Phase 1: index page → category URLs ───────────────────────────
        print(f"[{_SITE_ID}] fetching index page…")
        index_html = _curl(_INDEX_URL)
        if not index_html:
            print(f"[{_SITE_ID}] failed to fetch index page; aborting")
            return 0

        category_urls = _get_category_urls(index_html)
        print(f"[{_SITE_ID}] found {len(category_urls)} category URLs from index")

        # ── Phase 2: category pages (Level 1) → sub-pages (Level 2) ──────
        for cat_url in category_urls:
            if saved >= limit_val:
                break
            if (time.time() - start_time) > _BUDGET_SECONDS:
                print(f"[{_SITE_ID}] 25-min wall-clock budget reached; stopping")
                break
            if page_num >= _MAX_PAGES:
                print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached; stopping")
                break

            page_num += 1
            if page_num % 10 == 0:
                print(f"[{_SITE_ID}] page {page_num}: saved {saved}/{limit_str}")

            if cat_url in seen_urls:
                continue
            seen_urls.add(cat_url)

            # Fetch category page
            try:
                cat_html = _curl(cat_url)
                if not cat_html:
                    print(f"[{_SITE_ID}] item {cat_url} failed: no response")
                    continue
                time.sleep(_DELAY)

                cat_soup = _parse_html(cat_html)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] item {cat_url} failed: {exc}")
                continue

            # Check if the category page itself is a leaf publication
            try:
                pub = _extract_publication(cat_soup, cat_url)
                if pub:
                    self._save_paper(pub)
                    saved += 1
                    if saved >= limit_val:
                        break
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] save failed for {cat_url}: {exc}")

            # Collect sub-links from category page
            sub_links = _get_sub_links(cat_soup, seen_urls)

            # ── Level 2: process sub-links ─────────────────────────────
            for sub_url in sub_links:
                if saved >= limit_val:
                    break
                if (time.time() - start_time) > _BUDGET_SECONDS:
                    print(f"[{_SITE_ID}] 25-min wall-clock budget reached; stopping")
                    break
                if page_num >= _MAX_PAGES:
                    print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached; stopping")
                    break

                page_num += 1
                if page_num % 10 == 0:
                    print(f"[{_SITE_ID}] page {page_num}: saved {saved}/{limit_str}")

                if sub_url in seen_urls:
                    continue
                seen_urls.add(sub_url)

                try:
                    sub_html = _curl(sub_url)
                    if not sub_html:
                        print(f"[{_SITE_ID}] item {sub_url} failed: no response")
                        continue
                    time.sleep(_DELAY)

                    sub_soup = _parse_html(sub_html)
                    pub = _extract_publication(sub_soup, sub_url)
                    if pub:
                        self._save_paper(pub)
                        saved += 1
                    else:
                        # If still no abstract, try one more level deep
                        deep_links = _get_sub_links(sub_soup, seen_urls)
                        for deep_url in deep_links[:20]:  # cap at 20 per page
                            if saved >= limit_val:
                                break
                            if page_num >= _MAX_PAGES:
                                break
                            if deep_url in seen_urls:
                                continue
                            seen_urls.add(deep_url)
                            page_num += 1
                            try:
                                deep_html = _curl(deep_url)
                                if not deep_html:
                                    continue
                                time.sleep(_DELAY)
                                deep_soup = _parse_html(deep_html)
                                dpub = _extract_publication(deep_soup, deep_url)
                                if dpub:
                                    self._save_paper(dpub)
                                    saved += 1
                            except KeyboardInterrupt:
                                raise
                            except Exception as exc:
                                print(f"[{_SITE_ID}] item {deep_url} failed: {exc}")
                                continue

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {sub_url} failed: {exc}")
                    continue

        print(f"[{_SITE_ID}] done. saved {saved} records (visited {page_num} pages)")
        return saved
