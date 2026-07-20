# -*- coding: utf-8 -*-
"""CNES Communiqués crawler — https://cnes.fr/communiques"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

# absolute import — spec_from_file_location has no package context
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
from crawler.base_crawler import BaseCrawler

_FRENCH_MONTHS = {
    'janvier': '01', 'février': '02', 'mars': '03', 'avril': '04',
    'mai': '05', 'juin': '06', 'juillet': '07', 'août': '08',
    'septembre': '09', 'octobre': '10', 'novembre': '11', 'décembre': '12',
}

_LIST_URL = ('https://cnes.fr/communiques'
             '?display_parameters%5Bdisplay_type%5D=0')
_MAX_PAGES = 200
_BUDGET_SECONDS = 25 * 60


def _parse_french_date(s):
    """Parse '07 mai 2026' -> '2026-05-07', or None on failure."""
    if not s:
        return None
    m = re.match(r'(\d{1,2})\s+(\w+)\s+(\d{4})', s.strip().lower())
    if not m:
        return None
    day, month_fr, year = m.group(1), m.group(2), m.group(3)
    month = _FRENCH_MONTHS.get(month_fr)
    if not month:
        return None
    return f'{year}-{month}-{int(day):02d}'


def _curl_get(url, retries=3):
    """Fetch URL via curl with exponential-backoff retries. Returns text or None."""
    cmd = [
        'curl', '-skL', '--tls-max', '1.3', '--max-time', '30',
        '-H', 'Accept-Language: fr-FR,fr;q=0.9,en;q=0.8',
        '-H', ('User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
               'AppleWebKit/537.36 (KHTML, like Gecko) '
               'Chrome/120.0.0.0 Safari/537.36'),
        url,
    ]
    for attempt in range(retries):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=35)
            text = r.stdout.decode('utf-8', errors='replace')
            if text.strip():
                return text
        except Exception as exc:
            print(f'[cnes-fr-communiques] curl error attempt {attempt + 1}: {exc}')
        if attempt < retries - 1:
            wait = [1, 3, 9][attempt]
            print(f'[cnes-fr-communiques] retrying in {wait}s…')
            time.sleep(wait)
    return None


def _make_soup(html):
    """Parse HTML with html5lib → lxml → html.parser fallback chain."""
    from bs4 import BeautifulSoup
    for parser in ('html5lib', 'lxml', 'html.parser'):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _extract_body_text(soup):
    """Return cleaned body text from <main>, removing nav/aside/header/footer/form."""
    from bs4 import BeautifulSoup
    container = soup.find('main') or soup.find('body') or soup
    # Re-parse the container's HTML into a fresh tree so we don't mutate the original
    fresh = BeautifulSoup(str(container), 'html.parser')
    for tag in fresh.find_all(['nav', 'aside', 'header', 'footer', 'form', 'script', 'style']):
        tag.decompose()
    return fresh.get_text(separator=' ', strip=True)


class CnesFrCommuniquesCrawler(BaseCrawler):

    site_id = 'cnes-fr-communiques'
    site_name = 'Custom: cnes-fr-communiques'
    base_url = 'https://cnes.fr'

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        saved = 0
        seen_urls = set()
        page = 0

        while page < _MAX_PAGES:
            # 25-minute wall-clock budget
            if time.time() - start_time > _BUDGET_SECONDS:
                print(f'[cnes-fr-communiques] 25-minute budget reached at page {page}. Stopping.')
                break

            if limit is not None and saved >= limit:
                break

            # Fetch list page
            list_url = _LIST_URL + (f'&page={page}' if page > 0 else '')
            try:
                items = self._fetch_list_page(list_url, page)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f'[cnes-fr-communiques] list page {page} error: {exc}')
                break

            if not items:
                print(f'[cnes-fr-communiques] page {page}: no items. Done.')
                break

            # All items already seen → paginator looped back to page 0
            new_items = [it for it in items if it['url'] not in seen_urls]
            if not new_items:
                print(f'[cnes-fr-communiques] page {page}: all items already seen. '
                      'Stopping (pagination loop guard).')
                break

            for it in new_items:
                if limit is not None and saved >= limit:
                    break
                if it['url'] in seen_urls:
                    continue
                seen_urls.add(it['url'])

                try:
                    saved += self._process_item(it)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f'[cnes-fr-communiques] item {it["url"]} failed: {exc}')
                    continue

            if page > 0 and page % 10 == 0:
                lim_str = str(limit) if limit is not None else '∞'
                print(f'[cnes-fr-communiques] page {page}: saved {saved}/{lim_str}')

            page += 1
            time.sleep(self._delay)

        if page >= _MAX_PAGES:
            print(f'[cnes-fr-communiques] Safety cap of {_MAX_PAGES} pages reached.')

        print(f'[cnes-fr-communiques] Done. Total saved: {saved}')
        return saved

    # ------------------------------------------------------------------
    # List-page parsing
    # ------------------------------------------------------------------

    def _fetch_list_page(self, url, page_num):
        """Fetch one list page and return a list of item dicts."""
        raw = _curl_get(url)
        if not raw:
            print(f'[cnes-fr-communiques] failed to fetch list page {page_num}')
            return []

        try:
            soup = _make_soup(raw)
        except Exception as exc:
            print(f'[cnes-fr-communiques] soup error on list page {page_num}: {exc}')
            return []
        if soup is None:
            return []

        cards = soup.find_all(
            'article',
            class_=lambda c: c and 'fr-card' in (c if isinstance(c, str) else ' '.join(c)),
        )
        items = []
        for card in cards:
            try:
                a = card.find('a', class_='fr-card__link')
                if not a:
                    continue
                href = a.get('href', '')
                if not href.startswith('/communiques/'):
                    continue
                full_url = self.base_url + href
                slug = href.rstrip('/').rsplit('/', 1)[-1]
                title = a.get_text(strip=True)

                date_el = card.find(class_='fr-card__detail')
                listed_date_raw = date_el.get_text(strip=True) if date_el else ''
                listed_date = _parse_french_date(listed_date_raw)

                tags = [t.get_text(strip=True) for t in card.find_all(class_='fr-tag')]

                items.append({
                    'url': full_url,
                    'slug': slug,
                    'title': title,
                    'listed_date': listed_date,
                    'listed_date_raw': listed_date_raw,
                    'tags': tags,
                })
            except Exception as exc:
                print(f'[cnes-fr-communiques] card parse error: {exc}')
                continue
        return items

    # ------------------------------------------------------------------
    # Detail-page fetch + save
    # ------------------------------------------------------------------

    def _process_item(self, it):
        """Fetch detail page, build paper dict, save. Returns 1 on success, 0 on skip."""
        url = it['url']
        slug = it['slug']
        time.sleep(self._delay)

        raw = _curl_get(url)
        if not raw:
            print(f'[cnes-fr-communiques] failed to fetch detail: {url}')
            return 0

        try:
            soup = _make_soup(raw)
        except Exception as exc:
            print(f'[cnes-fr-communiques] soup error for {url}: {exc}')
            return 0
        if soup is None:
            return 0

        # Published date ("Publié le 12 février 2026")
        pub_date = None
        pub_match = re.search(r'Publié le (\d{1,2} \w+ \d{4})', raw)
        if pub_match:
            pub_date = _parse_french_date(pub_match.group(1))

        # Full body text
        abstract = ''
        try:
            abstract = _extract_body_text(soup)
        except Exception as exc:
            print(f'[cnes-fr-communiques] body extract error for {url}: {exc}')

        # Fallback to og:description / meta description
        if len(abstract) < 50:
            for meta_attr, meta_val in [('property', 'og:description'), ('name', 'description')]:
                meta_tag = soup.find('meta', attrs={meta_attr: meta_val})
                if meta_tag:
                    candidate = meta_tag.get('content', '')
                    if len(candidate) > len(abstract):
                        abstract = candidate
                    break

        if len(abstract) < 50:
            print(f'[cnes-fr-communiques] abstract too short ({len(abstract)} chars) '
                  f'for {url} — skipping')
            return 0

        # PDF link (first .pdf href in the page)
        pdf_url = None
        original_filename = None
        pdf_match = re.search(r'href=["\']([^"\']*\.pdf[^"\']*)["\']', raw, re.I)
        if pdf_match:
            pdf_raw = pdf_match.group(1)
            pdf_url = pdf_raw if pdf_raw.startswith('http') else self.base_url + pdf_raw
            original_filename = pdf_url.rstrip('/').rsplit('/', 1)[-1].split('?')[0]

        # Tags → category + keywords
        tags = it.get('tags', [])
        category = tags[0] if tags else None
        keywords = ','.join(tags[1:]) if len(tags) > 1 else None

        paper = {
            'site_id': self.site_id,
            'external_id': slug,
            'post_number': slug,
            'title': it['title'],
            'abstract': abstract,
            'url': url,
            'pdf_url': pdf_url,
            'original_filename': original_filename,
            'published_date': pub_date or it.get('listed_date'),
            'listed_date': it.get('listed_date'),
            'category': category,
            'keywords': keywords,
            'publisher': 'CNES',
            'authors': None,
            'doi': None,
            'department': None,
            'journal': None,
            'metadata': json.dumps({
                'posted_date': it.get('listed_date_raw'),
                'tags': tags,
                'slug': slug,
                'originalFilename': original_filename,
            }, ensure_ascii=False),
        }

        self._save_paper(paper)
        print(f'[cnes-fr-communiques] saved: {it["title"][:70]}')
        return 1
