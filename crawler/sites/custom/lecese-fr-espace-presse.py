# -*- coding: utf-8 -*-
"""lecese.fr — Espace presse (communiqués de presse) crawler.

Target:     https://www.lecese.fr/espace-presse
Pagination: ?page=N  (6 items/page, ~676 total → ~113 pages)
Detail:     https://www.lecese.fr/presse/communiques/SLUG
"""

import json
import os
import re
import subprocess
import time
from urllib.parse import unquote

from crawler.base_crawler import BaseCrawler


def _parse_dmy_date(text):
    """'14/04/2026' or '14-04-2026' → '2026-04-14', or ''."""
    if not text:
        return ''
    m = re.search(r'(\d{1,2})[/\-](\d{2})[/\-](\d{4})', text.strip())
    if m:
        return f'{m.group(3)}-{m.group(2)}-{int(m.group(1)):02d}'
    return ''


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


class LecesseFrEspacePressCrawler(BaseCrawler):
    """Crawler for CESE (Conseil Économique Social et Environnemental) press releases."""

    site_id = 'lecese-fr-espace-presse'
    site_name = 'Custom: lecese-fr-espace-presse'
    base_url = 'https://www.lecese.fr'

    _LIST_URL = 'https://www.lecese.fr/espace-presse'
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _SKIP_ABSTRACT_LEN = 100

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
        """Return list of item dicts from a listing page.

        Each dict: node_id, title, url, listed_date_raw, subtitle, pdf_url
        """
        soup = _make_soup(html)
        if soup is not None:
            return self._parse_list_soup(soup)
        return self._parse_list_regex(html)

    def _parse_list_soup(self, soup):
        items = []
        for node_div in soup.find_all('div', attrs={'data-history-node-id': True}):
            node_id = node_div.get('data-history-node-id', '')

            # Title + URL
            a_tag = node_div.find('a', href=re.compile(r'/presse/communiques/'))
            if not a_tag:
                continue
            href = a_tag['href']
            if not href.startswith('http'):
                href = self.base_url + href
            title = a_tag.get_text(strip=True)

            # Date (DD/MM/YYYY on list page)
            date_raw = ''
            date_div = node_div.find(class_=re.compile(r'field--name-field-date'))
            if date_div:
                date_raw = date_div.get_text(strip=True)

            # Subtitle
            sub_div = node_div.find(class_=re.compile(r'field--name-field-sous-titre'))
            subtitle = sub_div.get_text(strip=True) if sub_div else ''

            # PDF link on list page (saves a detail fetch)
            pdf_url = None
            original_filename = None
            pdf_div = node_div.find(class_=re.compile(r'field--name-field-file-unik-1'))
            if pdf_div:
                pdf_a = pdf_div.find('a', href=re.compile(r'\.pdf', re.IGNORECASE))
                if pdf_a:
                    pdf_href = pdf_a['href']
                    if not pdf_href.startswith('http'):
                        pdf_href = self.base_url + pdf_href
                    pdf_url = pdf_href
                    original_filename = unquote(pdf_href.rstrip('/').split('/')[-1])

            if title:
                items.append({
                    'node_id': node_id,
                    'title': title,
                    'url': href,
                    'listed_date_raw': date_raw,
                    'subtitle': subtitle,
                    'pdf_url': pdf_url,
                    'original_filename': original_filename,
                })
        return items

    def _parse_list_regex(self, html):
        """Regex fallback for listing pages."""
        items = []
        for node_m in re.finditer(
            r'data-history-node-id="(\d+)"(.*?)(?=data-history-node-id="|<div class="view-footer")',
            html, re.DOTALL
        ):
            node_id = node_m.group(1)
            chunk = node_m.group(2)

            href_m = re.search(r'href="(/presse/communiques/[^"?#]+)"', chunk)
            if not href_m:
                continue
            href = self.base_url + href_m.group(1)
            title_m = re.search(r'field--name-node-title[^>]*>.*?<a[^>]+>([^<]+)</a>', chunk, re.DOTALL)
            title = _strip_tags(title_m.group(1)) if title_m else ''

            date_m = re.search(r'field--name-field-date[^>]*>.*?(\d{1,2}/\d{2}/\d{4})', chunk, re.DOTALL)
            date_raw = date_m.group(1) if date_m else ''

            sub_m = re.search(r'field--name-field-sous-titre[^>]*>\s*<[^>]+>([^<]+)<', chunk, re.DOTALL)
            subtitle = _strip_tags(sub_m.group(1)) if sub_m else ''

            pdf_url = None
            original_filename = None
            pdf_m = re.search(r'field--name-field-file-unik-1.*?href="([^"]+\.pdf)"', chunk, re.DOTALL | re.IGNORECASE)
            if pdf_m:
                ph = pdf_m.group(1)
                if not ph.startswith('http'):
                    ph = self.base_url + ph
                pdf_url = ph
                original_filename = unquote(ph.rstrip('/').split('/')[-1])

            if title:
                items.append({
                    'node_id': node_id,
                    'title': title,
                    'url': href,
                    'listed_date_raw': date_raw,
                    'subtitle': subtitle,
                    'pdf_url': pdf_url,
                    'original_filename': original_filename,
                })
        return items

    # ------------------------------------------------------------------
    # Detail-page parsing
    # ------------------------------------------------------------------

    def _parse_detail_abstract(self, html):
        """Return abstract text from the detail page body field."""
        soup = _make_soup(html)
        if soup is not None:
            return self._parse_detail_soup(soup)
        return self._parse_detail_regex(html)

    def _parse_detail_soup(self, soup):
        """Extract abstract from field-cke-unik-1 on the detail page."""
        parts = []

        # Primary: field-cke-unik-1 (main article body)
        for cls in ('field--name-field-cke-unik-1', 'field-cke-unik-1',
                    'field--name-body', 'field--name-field-cke-unik-2'):
            body_div = soup.find(class_=re.compile(cls))
            if body_div:
                for tag in body_div.find_all(['p', 'li']):
                    txt = tag.get_text(separator=' ', strip=True)
                    if txt and len(txt) > 20:
                        parts.append(txt)
                if parts:
                    break

        # Fallback: any substantial paragraph in main content
        if len('\n'.join(parts)) < 100:
            main = soup.find(id='main') or soup.find(id='content') or soup
            seen = set(parts)
            bad_ancestors = {'footer', 'header', 'nav', 'sidebar', 'menu', 'breadcrumb'}
            for p in main.find_all('p'):
                txt = p.get_text(separator=' ', strip=True)
                if len(txt) < 60 or txt in seen:
                    continue
                parent_cls = ' '.join(
                    ' '.join(anc.get('class', []))
                    for anc in p.parents if anc.get('class')
                )
                if any(x in parent_cls for x in bad_ancestors):
                    continue
                # Skip copyright / footer lines
                if re.search(r'copyright|tous droits|lecese\.fr|CESE\. Tous', txt, re.IGNORECASE):
                    continue
                parts.append(txt)
                seen.add(txt)
                if len('\n\n'.join(parts)) > 4000:
                    break

        return '\n\n'.join(parts)

    def _parse_detail_regex(self, html):
        """Regex fallback for detail page body extraction."""
        m = re.search(
            r'field--name-field-cke-unik-1.*?(<p.*?</div>\s*</div>)',
            html, re.DOTALL | re.IGNORECASE
        )
        if not m:
            m = re.search(r'field--name-body(.*?)(?=field--|</article)', html, re.DOTALL)
        if not m:
            return ''
        chunk = m.group(1)
        paras = re.findall(r'<p[^>]*>(.*?)</p>', chunk, re.DOTALL)
        parts = [
            _strip_tags(p).strip() for p in paras
            if len(_strip_tags(p).strip()) > 20
        ]
        return '\n\n'.join(parts)

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl CESE press releases page by page."""
        saved = 0
        seen_urls = set()
        deadline = time.time() + int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute wall-clock cap
        limit_str = str(limit) if limit is not None else '∞'

        for page in range(self._MAX_PAGES):
            if limit is not None and saved >= limit:
                break
            if time.time() > deadline:
                print(f'[{self.site_id}] 25-minute budget reached at page {page}. Exiting.')
                break

            list_url = f'{self._LIST_URL}?page={page}'
            raw = self._curl_get(list_url)
            if not raw:
                print(f'[{self.site_id}] Failed to fetch page {page}. Stopping.')
                break

            items = self._parse_list_page(raw)
            if not items:
                print(f'[{self.site_id}] No items at page {page}. Done.')
                break

            new_items = [it for it in items if it['url'] not in seen_urls]
            if not new_items and page > 0:
                print(f'[{self.site_id}] No new items at page {page} (pagination loop). Done.')
                break

            if page % 10 == 0:
                print(f'[{self.site_id}] page {page}: saved {saved}/{limit_str}')

            for item in items:
                if limit is not None and saved >= limit:
                    break

                url = item['url']
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                try:
                    listed_date = _parse_dmy_date(item.get('listed_date_raw', ''))
                    node_id = item.get('node_id', '')
                    slug = url.rstrip('/').split('/')[-1]

                    # Fetch detail page for abstract
                    time.sleep(self._delay)
                    detail_html = self._curl_get(url)
                    if not detail_html:
                        print(f'[{self.site_id}] detail fetch failed: {url}')
                        continue

                    abstract = self._parse_detail_abstract(detail_html).strip()

                    # Supplement with subtitle if abstract is short
                    if len(abstract) < self._SKIP_ABSTRACT_LEN:
                        subtitle = item.get('subtitle', '').strip()
                        if subtitle:
                            abstract = (
                                (abstract + '\n\n' + subtitle).strip()
                                if abstract else subtitle
                            )

                    if len(abstract) < 50:
                        print(f'[{self.site_id}] abstract <50 chars for {url}, skipping')
                        continue

                    pdf_url = item.get('pdf_url')
                    original_filename = item.get('original_filename')
                    if pdf_url and not original_filename:
                        original_filename = unquote(pdf_url.rstrip('/').split('/')[-1])

                    paper = {
                        'id': None,
                        'site_id': self.site_id,
                        'external_id': node_id or slug,
                        'post_number': node_id or slug,
                        'title': item['title'],
                        'abstract': abstract,
                        'published_date': listed_date,
                        'listed_date': listed_date,
                        'authors': '',
                        'publisher': 'Conseil Économique Social et Environnemental (CESE)',
                        'department': '',
                        'journal': '',
                        'url': url,
                        'pdf_url': pdf_url,
                        'keywords': '',
                        'category': '',
                        'doi': '',
                        'original_filename': original_filename,
                        'metadata': json.dumps({
                            'posted_date': item.get('listed_date_raw', ''),
                            'slug': slug,
                            'node_id': node_id,
                            'subtitle': item.get('subtitle', ''),
                            'originalFilename': original_filename,
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
