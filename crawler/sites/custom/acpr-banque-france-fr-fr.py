# -*- coding: utf-8 -*-
"""ACPR Banque de France — études et recherche crawler.

Target: https://acpr.banque-france.fr/fr/publications-et-statistiques/etudes-et-recherche
Filters: Analyses et synthèses (format 5412591) + Discussion papers (format 5412600)
Pagination: ?page=N  (~10 items/page, pages 0–~18)
"""

import json
import os
import re
import subprocess
import time
from urllib.parse import unquote

from crawler.base_crawler import BaseCrawler

# ---------------------------------------------------------------------------
# French month → zero-padded number
# ---------------------------------------------------------------------------
_FRENCH_MONTHS = {
    'janvier': '01', 'février': '02', 'fevrier': '02',
    'mars': '03', 'avril': '04', 'mai': '05', 'juin': '06',
    'juillet': '07', 'août': '08', 'aout': '08',
    'septembre': '09', 'octobre': '10', 'novembre': '11', 'décembre': '12',
    'decembre': '12',
}


def _parse_french_date(text):
    """'27 Avril 2026' → '2026-04-27', '26/03/2026' → '2026-03-26', or ''."""
    if not text:
        return ''
    # dd/mm/yyyy format (from Drupal JSON)
    m = re.match(r'(\d{2})/(\d{2})/(\d{4})', text.strip())
    if m:
        return f'{m.group(3)}-{m.group(2)}-{m.group(1)}'
    # French "DD Mois YYYY"
    m = re.search(r'(\d{1,2})\s+(\w+)\s+(\d{4})', text.lower())
    if not m:
        return ''
    day, month_str, year = m.group(1), m.group(2), m.group(3)
    month_norm = (month_str
                  .replace('é', 'e').replace('è', 'e')
                  .replace('û', 'u').replace('ü', 'u')
                  .replace('à', 'a').replace('â', 'a'))
    month = _FRENCH_MONTHS.get(month_str) or _FRENCH_MONTHS.get(month_norm)
    if not month:
        return ''
    return f'{year}-{month}-{int(day):02d}'


# ---------------------------------------------------------------------------
# HTML helpers
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


def _strip_tags(s):
    text = re.sub(r'<[^>]+>', ' ', s or '')
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&[a-zA-Z0-9#]+;', '', text)
    return re.sub(r'\s+', ' ', text).strip()


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class ACPRBanqueFranceCrawler(BaseCrawler):
    """Crawler for ACPR études et recherche publications."""

    site_id = 'acpr-banque-france-fr-fr'
    site_name = 'Custom: acpr-banque-france-fr-fr'
    base_url = 'https://acpr.banque-france.fr'

    _LIST_URL = (
        'https://acpr.banque-france.fr/fr/publications-et-statistiques/etudes-et-recherche'
        '?search_publications_api_fulltext='
        '&format%5B5412591%5D=5412591'
        '&format%5B5412600%5D=5412600'
        '&start-date_year=&start-date_month=&end-date_year=&end-date_month='
    )
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _SKIP_ABSTRACT_LEN = 100  # skip items where abstract < this (matches test assertion >=100)

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url):
        """GET via curl with 3 retries (1s, 3s, 9s backoff). Returns str or None."""
        cmd = [
            'curl', '--tls-max', '1.3', '-skL', '--max-time', '30',
            '-H', 'Accept: text/html,application/xhtml+xml,*/*;q=0.8',
            '-H', 'Accept-Language: fr-FR,fr;q=0.9,en;q=0.8',
            '-H', f'User-Agent: {self.USER_AGENT}',
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=40)
                text = result.stdout.decode('utf-8', errors='replace')
                if text.strip():
                    return text
                wait = [1, 3, 9][attempt]
                print(f'[{self.site_id}] empty response, retrying in {wait}s...')
                time.sleep(wait)
            except Exception as exc:
                wait = [1, 3, 9][attempt]
                print(f'[{self.site_id}] curl error: {exc}, retrying in {wait}s...')
                if attempt < 2:
                    time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # List-page parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, html):
        """Return list of item dicts from a listing page."""
        soup = _make_soup(html)
        if soup is not None:
            return self._parse_list_soup(soup)
        return self._parse_list_regex(html)

    def _parse_list_soup(self, soup):
        items = []
        # Cards are <a class="card card-vertical ..."> directly (href on the anchor)
        for card in soup.find_all('a', class_=re.compile(r'\bcard-vertical\b')):
            href = card.get('href', '').strip()
            # Filter to publication detail pages only
            if not href or '/publications/' not in href:
                continue
            if not href.startswith('http'):
                href = self.base_url + href

            title_el = card.find(class_=re.compile(r'\bcard-title\b'))
            title = title_el.get_text(strip=True) if title_el else ''
            if not title:
                title = card.get_text(strip=True)[:120]

            cat_el = card.find(class_=re.compile(r'\bcategory-btn-grid\b'))
            category = (re.sub(r'\s+', ' ', cat_el.get_text(strip=True)).strip()
                        if cat_el else '')

            date_str = ''
            for small in card.find_all('small'):
                txt = small.get_text(strip=True)
                if re.search(r'\d{4}', txt) and len(txt) < 60:
                    date_str = txt
                    break

            if title:
                items.append({
                    'title': title,
                    'url': href,
                    'category': category,
                    'listed_date_raw': date_str,
                    'teaser': '',
                })
        return items

    def _parse_list_regex(self, html):
        """Regex fallback for listing pages."""
        items = []
        for m in re.finditer(
            r'card-vertical.*?(?=card-vertical|</main>|\Z)',
            html, re.DOTALL
        ):
            c = m.group(0)
            href_m = re.search(
                r'href\s*=\s*["\s]*(/fr/publications-et-statistiques/publications/[^"?#\s]+)',
                c)
            title_m = re.search(
                r'class="card-title[^"]*">\s*(.*?)\s*</h', c, re.DOTALL)
            if not href_m or not title_m:
                continue
            href = self.base_url + href_m.group(1).strip()
            title = _strip_tags(title_m.group(1))
            cat_m = re.search(r'category-btn-grid[^>]*>(.*?)</span>', c, re.DOTALL)
            category = _strip_tags(cat_m.group(1)) if cat_m else ''
            date_str = ''
            for s in re.findall(r'<small[^>]*>(.*?)</small>', c, re.DOTALL):
                t = _strip_tags(s).strip()
                if re.search(r'\d{4}', t) and len(t) < 60:
                    date_str = t
            if title:
                items.append({
                    'title': title,
                    'url': href,
                    'category': category,
                    'listed_date_raw': date_str,
                    'teaser': '',
                })
        return items

    # ------------------------------------------------------------------
    # Detail-page parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, html):
        """Return dict: abstract, pdf_url, published_date, original_filename."""
        soup = _make_soup(html)
        if soup is not None:
            return self._parse_detail_soup(soup)
        return self._parse_detail_regex(html)

    def _parse_detail_soup(self, soup):
        result = {
            'abstract': '', 'pdf_url': None,
            'published_date': '', 'original_filename': None,
        }
        # Main content area — id='main-content' is a skip-link <a> anchor, not the container
        main = (soup.find('article', class_=re.compile(r'\bnode\b'))
                or soup.find('main')
                or soup.find(id='main')
                or soup.find(id='content')
                or soup)

        # Published date from Drupal settings JSON (most reliable for études)
        for script in soup.find_all('script',
                                    attrs={'data-drupal-selector': 'drupal-settings-json'}):
            try:
                data = json.loads(script.string or '')
                raw_date = (data.get('piano_props', {})
                               .get('custom_publication_statistic_date', ''))
                if raw_date:
                    d = _parse_french_date(raw_date)
                    if d:
                        result['published_date'] = d
                        break
            except Exception:
                pass

        # Main body field — try several known Drupal field class names
        body_div = None
        for cls in (
            'field--name-field-contenu-riche',
            'field-contenu-riche',
            'field--name-body',
            'field--type-text-long',
        ):
            body_div = main.find(class_=re.compile(cls))
            if body_div:
                break

        parts = []
        if body_div:
            for tag in body_div.find_all(['p', 'li']):
                txt = tag.get_text(separator=' ', strip=True)
                if txt and len(txt) > 20:
                    if not re.match(r'^\[\d+\]\s*$', txt):
                        parts.append(txt)

        # Supplement with header/description field if body is short
        if len('\n'.join(parts)) < 200:
            for cls in (
                'field--name-field-espaces2-header-text',
                'field-espaces2-description',
                'field--name-field-espaces2-description',
                'field--name-field-description',
            ):
                desc_div = main.find(class_=re.compile(cls))
                if desc_div:
                    txt = desc_div.get_text(separator=' ', strip=True)
                    if txt and txt not in parts:
                        parts.insert(0, txt)
                    break

        # Last-resort: any substantive paragraph in main content
        if len('\n'.join(parts)) < 100:
            seen_txts = set(parts)
            bad_cls = {'footer', 'header', 'nav', 'menu', 'sidebar',
                       'breadcrumb', 'region-secondary', 'dashboard'}
            for p in main.find_all('p'):
                txt = p.get_text(separator=' ', strip=True)
                if len(txt) < 60 or txt in seen_txts:
                    continue
                if re.match(r'^(Mise en ligne|Aller au|Retour)', txt, re.IGNORECASE):
                    continue
                parent_cls_str = ' '.join(
                    ' '.join(anc.get('class', []))
                    for anc in p.parents if anc.get('class')
                )
                if any(x in parent_cls_str for x in bad_cls):
                    continue
                parts.append(txt)
                seen_txts.add(txt)
                if len('\n\n'.join(parts)) > 3000:
                    break

        result['abstract'] = '\n\n'.join(parts)

        # Published date fallbacks
        if not result['published_date']:
            for tag in main.find_all(string=re.compile(r'Mise en ligne', re.IGNORECASE)):
                d = _parse_french_date(str(tag))
                if d:
                    result['published_date'] = d
                    break
        if not result['published_date']:
            for tag in main.find_all(['small', 'p', 'div', 'span']):
                txt = tag.get_text(strip=True)
                if re.search(r'\d{4}', txt) and len(txt) < 60:
                    d = _parse_french_date(txt)
                    if d:
                        result['published_date'] = d
                        break

        # PDF links — prefer card-download anchors with data-file-extension
        for a in main.find_all('a', attrs={'data-file-extension': 'pdf'}):
            h = a.get('href', '')
            if h:
                if not h.startswith('http'):
                    h = self.base_url + h
                result['pdf_url'] = h
                result['original_filename'] = unquote(h.rstrip('/').split('/')[-1])
                break
        # Fallback: any .pdf href
        if not result['pdf_url']:
            for a in main.find_all('a', href=True):
                h = a['href']
                if h.lower().endswith('.pdf'):
                    if not h.startswith('http'):
                        h = self.base_url + h
                    result['pdf_url'] = h
                    result['original_filename'] = unquote(h.rstrip('/').split('/')[-1])
                    break

        return result

    def _parse_detail_regex(self, html):
        """Regex fallback for detail pages."""
        result = {
            'abstract': '', 'pdf_url': None,
            'published_date': '', 'original_filename': None,
        }
        # Published date from Drupal JSON
        m_json = re.search(
            r'custom_publication_statistic_date["\s:]+([^"]+)"', html)
        if m_json:
            raw = m_json.group(1).replace('\\/', '/')
            result['published_date'] = _parse_french_date(raw)
        # Body field
        for pat in (
            r'field--name-field-contenu-riche[^>]*>.*?<body>(.*?)</body>',
            r'field--name-field-contenu-riche[^>]*>(.*?)(?=</div>\s*</div>)',
            r'field-contenu-riche[^>]*>(.*?)(?=</div>\s*</div>)',
        ):
            m = re.search(pat, html, re.DOTALL | re.IGNORECASE)
            if m:
                paras = re.findall(r'<p[^>]*>(.*?)</p>', m.group(1), re.DOTALL)
                parts = [
                    _strip_tags(p).strip() for p in paras
                    if len(_strip_tags(p).strip()) > 20
                    and not re.match(r'^\[\d+\]\s*$', _strip_tags(p).strip())
                ]
                result['abstract'] = '\n\n'.join(parts)
                break
        # Header text supplement
        if len(result['abstract']) < 100:
            m_hdr = re.search(
                r'field-espaces2-header-text[^>]*>(.*?)</div>', html,
                re.DOTALL | re.IGNORECASE)
            if m_hdr:
                hdr = _strip_tags(m_hdr.group(1)).strip()
                if hdr:
                    result['abstract'] = (
                        (hdr + '\n\n' + result['abstract']).strip()
                        if result['abstract'] else hdr
                    )
        # Date fallback
        if not result['published_date']:
            dm = re.search(
                r'Mise en ligne le\s+(\d{1,2}\s+\w+\s+\d{4})', html, re.IGNORECASE)
            if dm:
                result['published_date'] = _parse_french_date(dm.group(1))
        # PDF
        pm = re.search(r'href\s*=\s*["\s]*([^"\s]*\.pdf)', html, re.IGNORECASE)
        if pm:
            h = pm.group(1).strip()
            if not h.startswith('http'):
                h = self.base_url + h
            result['pdf_url'] = h
            result['original_filename'] = unquote(h.rstrip('/').split('/')[-1])
        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl ACPR études et recherche page by page."""
        saved = 0
        seen_urls = set()
        deadline = time.time() + int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute wall-clock cap

        for page in range(self._MAX_PAGES):
            if limit is not None and saved >= limit:
                break
            if time.time() > deadline:
                print(f'[{self.site_id}] 25-minute budget reached at page {page}. Exiting.')
                break

            list_url = f'{self._LIST_URL}&page={page}'
            raw = self._curl_get(list_url)
            if not raw:
                print(f'[{self.site_id}] Failed to fetch page {page}. Stopping.')
                break

            items = self._parse_list_page(raw)
            if not items:
                print(f'[{self.site_id}] No items at page {page}. Done.')
                break

            # All URLs already seen → pagination looped back to page 1
            new_items = [it for it in items if it['url'] not in seen_urls]
            if not new_items and page > 0:
                print(f'[{self.site_id}] No new items at page {page}. Done.')
                break

            if page % 10 == 0:
                lim_str = str(limit) if limit is not None else '∞'
                print(f'[{self.site_id}] page {page}: saved {saved}/{lim_str}')

            for item in items:
                if limit is not None and saved >= limit:
                    break

                url = item['url']
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                try:
                    listed_date = _parse_french_date(item.get('listed_date_raw', ''))

                    time.sleep(self._delay)
                    detail_html = self._curl_get(url)
                    if not detail_html:
                        print(f'[{self.site_id}] detail fetch failed: {url}')
                        continue

                    detail = self._parse_detail(detail_html)
                    abstract = detail['abstract'].strip()

                    # Supplement short abstract with teaser if available
                    if len(abstract) < self._SKIP_ABSTRACT_LEN:
                        teaser = item.get('teaser', '').strip()
                        if teaser:
                            abstract = (
                                (abstract + '\n\n' + teaser).strip()
                                if abstract else teaser
                            )

                    if len(abstract) < 50:
                        print(f'[{self.site_id}] abstract <50 chars for {url}, skipping')
                        continue

                    published_date = detail['published_date'] or listed_date
                    slug = url.rstrip('/').split('/')[-1]
                    pdf_url = detail['pdf_url']
                    original_filename = detail['original_filename']
                    if pdf_url and not original_filename:
                        original_filename = unquote(pdf_url.rstrip('/').split('/')[-1])

                    paper = {
                        'id': None,
                        'site_id': self.site_id,
                        'external_id': slug,
                        'post_number': slug,
                        'title': item['title'],
                        'abstract': abstract,
                        'published_date': published_date,
                        'listed_date': listed_date,
                        'authors': '',
                        'publisher': 'ACPR - Autorité de contrôle prudentiel et de résolution',
                        'url': url,
                        'pdf_url': pdf_url,
                        'keywords': '',
                        'category': item.get('category', ''),
                        'doi': '',
                        'department': '',
                        'original_filename': original_filename,
                        'metadata': json.dumps({
                            'posted_date': item.get('listed_date_raw', ''),
                            'slug': slug,
                            'category': item.get('category', ''),
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    ctr = f'{saved}/{limit}' if limit is not None else str(saved)
                    print(f'[{self.site_id}] Saved {ctr}: {item["title"][:60]}')

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f'[{self.site_id}] item failed [{url}]: {exc}')
                    continue

            if page == self._MAX_PAGES - 1:
                print(f'[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached.')
                break

        print(f'[{self.site_id}] Done. Total saved: {saved}')
        return saved
