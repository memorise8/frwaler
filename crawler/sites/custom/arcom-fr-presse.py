# -*- coding: utf-8 -*-
"""Arcom France presse crawler — communiqués de presse (field_type_de_presse=16).

Target: https://www.arcom.fr/presse?field_type_de_presse_target_id%5B16%5D=16&...
"""

import json
import os
import re
import subprocess
import time
from urllib.parse import unquote, urlparse

from crawler.base_crawler import BaseCrawler

# ---------------------------------------------------------------------------
# French month → zero-padded number
# ---------------------------------------------------------------------------
_FRENCH_MONTHS = {
    'janvier': '01', 'février': '02', 'mars': '03', 'avril': '04',
    'mai': '05', 'juin': '06', 'juillet': '07', 'août': '08',
    'septembre': '09', 'octobre': '10', 'novembre': '11', 'décembre': '12',
}


def _parse_french_date(text):
    """'DD mois YYYY' → 'YYYY-MM-DD', or None."""
    if not text:
        return None
    m = re.search(r'(\d{1,2})\s+(\w+)\s+(\d{4})', text.lower())
    if not m:
        return None
    day, month_str, year = m.group(1), m.group(2), m.group(3)
    month = _FRENCH_MONTHS.get(month_str)
    if not month:
        return None
    return f"{year}-{month}-{int(day):02d}"


# ---------------------------------------------------------------------------
# curl helper
# ---------------------------------------------------------------------------
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _curl_get(url, retries=3):
    """GET via curl; returns response text (str) or None."""
    cmd = [
        'curl', '--tls-max', '1.3', '-skL', '--max-time', '30',
        '-H', 'Accept: text/html,application/xhtml+xml,*/*;q=0.8',
        '-H', 'Accept-Language: fr-FR,fr;q=0.9,en;q=0.8',
        '-H', f'User-Agent: {_UA}',
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=40)
            text = result.stdout.decode('utf-8', errors='replace')
            if text.strip():
                return text
            if attempt < retries - 1:
                time.sleep((attempt + 1) * 3)
        except Exception as exc:
            if attempt < retries - 1:
                time.sleep((attempt + 1) * 3)
            else:
                print(f'[arcom-fr-presse] curl failed: {exc}')
    return None


# ---------------------------------------------------------------------------
# BeautifulSoup with parser fallback
# ---------------------------------------------------------------------------

def _make_soup(html):
    """Try html5lib → lxml → html.parser; return BeautifulSoup or None."""
    from bs4 import BeautifulSoup
    for parser in ('html5lib', 'lxml', 'html.parser'):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class ArcomFrPresseCrawler(BaseCrawler):
    site_id = 'arcom-fr-presse'
    site_name = 'Custom: arcom-fr-presse'
    base_url = 'https://www.arcom.fr'

    _LIST_BASE = (
        'https://www.arcom.fr/presse'
        '?field_type_de_presse_target_id%5B16%5D=16'
        '&field_date_de_decision_value_1='
        '&field_date_de_decision_value='
        '&sort_bef_combine=field_date_de_decision_value_ASC'
    )

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        limit_or_inf = limit if limit is not None else float('inf')
        start_time = time.time()
        MAX_WALL = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes in seconds
        MAX_PAGES = 200

        page = 0
        while page < MAX_PAGES:
            # Guard: limit reached
            if saved >= limit_or_inf:
                break

            # Guard: wall-clock budget
            if time.time() - start_time > MAX_WALL:
                print(f'[arcom-fr-presse] Wall-clock budget reached at page {page}; exiting cleanly.')
                break

            list_url = self._LIST_BASE + f'&page={page}'
            html = _curl_get(list_url)
            if not html:
                print(f'[arcom-fr-presse] Failed to fetch listing page {page}; stopping.')
                break

            soup = _make_soup(html)
            if not soup:
                print(f'[arcom-fr-presse] Failed to parse listing page {page}; stopping.')
                break

            # Collect detail links from views-row elements only
            new_links = []
            for a in soup.find_all('a', href=True):
                href = a['href']
                # Must be /presse/<slug> with no query params or fragments
                if (href.startswith('/presse/')
                        and '?' not in href
                        and '#' not in href
                        and len(href) > len('/presse/')):
                    full_url = self.base_url + href
                    if full_url not in seen_urls:
                        seen_urls.add(full_url)
                        slug = href[len('/presse/'):]
                        new_links.append((slug, full_url))

            if not new_links:
                print(f'[arcom-fr-presse] No new links on page {page}; pagination complete.')
                break

            if page % 10 == 0:
                lim_str = str(limit) if limit is not None else '∞'
                print(f'[arcom-fr-presse] page {page}: saved {saved}/{lim_str}')

            for slug, detail_url in new_links:
                if saved >= limit_or_inf:
                    break
                if time.time() - start_time > MAX_WALL:
                    break

                try:
                    paper = self._fetch_detail(slug, detail_url)
                    if paper is None:
                        continue
                    self._save_paper(paper)
                    saved += 1
                    time.sleep(self._delay)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f'[arcom-fr-presse] item {slug} failed: {exc}')
                    continue

            page += 1

        if page >= MAX_PAGES:
            print(f'[arcom-fr-presse] Safety cap of {MAX_PAGES} pages reached; exiting.')

        return saved

    def _fetch_detail(self, slug, url):
        """Fetch + parse one detail page. Returns paper dict or None."""
        html = None
        for attempt in range(3):
            html = _curl_get(url)
            if html:
                break
            if attempt < 2:
                wait = (attempt + 1) * 3
                print(f'[arcom-fr-presse] Retry {attempt + 1}/3 for {slug}')
                time.sleep(wait)

        if not html:
            print(f'[arcom-fr-presse] Failed to fetch detail {slug} after 3 attempts; skipping.')
            return None

        soup = _make_soup(html)
        if not soup:
            print(f'[arcom-fr-presse] Failed to parse {slug}; skipping.')
            return None

        # -- Title --
        h1 = soup.find('h1')
        title = h1.get_text(separator=' ', strip=True) if h1 else slug

        # -- Date: "Publié le DD mois YYYY" in field-date-de-decision --
        published_date = None
        date_div = soup.find(class_=re.compile(r'field--name-field-date-de-decision'))
        if date_div:
            published_date = _parse_french_date(date_div.get_text())
        if not published_date:
            # Fallback: search whole page text
            m = re.search(r'Publi[ée]\s+le\s+(\d{1,2}\s+\w+\s+\d{4})', html, re.I)
            if m:
                published_date = _parse_french_date(m.group(1))

        # -- Category --
        category = None
        cat_div = soup.find(class_=re.compile(r'field--name-field-type-de-presse'))
        if cat_div:
            li = cat_div.find('li')
            if li:
                category = li.get_text(strip=True)

        # -- Abstract: field-presse-contenu → body text --
        abstract = self._extract_abstract(soup, title)
        if not abstract or len(abstract) < 100:
            print(
                f'[arcom-fr-presse] Skipping {slug}: abstract too short '
                f'({len(abstract) if abstract else 0} chars)'
            )
            return None

        # -- PDF links --
        pdf_url = None
        original_filename = None
        for a in soup.find_all('a', href=re.compile(r'\.pdf', re.I)):
            href = a['href']
            if href.startswith('/sites/default/files/') or href.startswith('http'):
                pdf_url = (self.base_url + href) if href.startswith('/') else href
                raw_name = unquote(urlparse(pdf_url).path.split('/')[-1])
                if raw_name:
                    original_filename = raw_name
                break

        metadata = {
            'slug': slug,
            'type_de_presse': category,
        }
        if category:
            metadata['posted_date'] = published_date

        return {
            'site_id': self.site_id,
            'external_id': slug,
            'post_number': slug,
            'title': title,
            'abstract': abstract,
            'published_date': published_date,
            'listed_date': published_date,
            'url': url,
            'pdf_url': pdf_url,
            'original_filename': original_filename,
            'category': category,
            'publisher': 'Arcom',
            'authors': None,
            'keywords': None,
            'doi': None,
            'department': None,
            'journal': None,
            'metadata': json.dumps(metadata, ensure_ascii=False),
        }

    def _extract_abstract(self, soup, title):
        """Extract article body text as abstract string."""
        # Primary: Drupal field--name-field-presse-contenu
        content_div = soup.find(class_=re.compile(r'field--name-field-presse-contenu'))
        if content_div:
            text = content_div.get_text(separator=' ', strip=True)
            text = re.sub(r'\s+', ' ', text).strip()
            if len(text) >= 100:
                return text

        # Fallback: .main-container or <main>, strip nav/header/footer
        for selector in (['class_', re.compile(r'main-container')], ['name', 'main']):
            if selector[0] == 'class_':
                container = soup.find(class_=selector[1])
            else:
                container = soup.find(selector[1])
            if not container:
                continue
            # Remove non-content elements
            for tag in container.find_all(['nav', 'header', 'footer', 'script', 'style']):
                tag.decompose()
            text = container.get_text(separator=' ', strip=True)
            text = re.sub(r'\s+', ' ', text).strip()
            # Strip leading breadcrumb/title noise
            if title and title in text:
                idx = text.find(title)
                text = text[idx + len(title):].strip()
            if len(text) >= 100:
                return text

        return None
