# -*- coding: utf-8 -*-
"""CentraleSupélec press releases crawler.

Target: https://www.centralesupelec.fr/presse/communiques
Strategy: parse Drupal 10 list pages (HTML, ?page=N), follow /node/<id>
redirects for full abstract, save original file links from the article teaser.
"""

import json
import os
import re
import subprocess
import time
from urllib.parse import unquote, urljoin, urlsplit

from crawler.base_crawler import BaseCrawler

_MONTHS_FR = {
    'janvier': '01', 'février': '02', 'mars': '03', 'avril': '04',
    'mai': '05', 'juin': '06', 'juillet': '07', 'août': '08',
    'septembre': '09', 'octobre': '10', 'novembre': '11', 'décembre': '12',
}


def _parse_fr_date(text):
    """Parse 'Publié le 06 mai 2026' → '2026-05-06'. Returns None on failure."""
    if not text:
        return None
    m = re.search(r'(\d{1,2})\s+(\w+)\s+(\d{4})', text)
    if not m:
        return None
    day, month_str, year = m.groups()
    month = _MONTHS_FR.get(month_str.lower())
    if not month:
        return None
    return f"{year}-{month}-{int(day):02d}"


def _make_soup(html):
    """Try html5lib → lxml → html.parser; return BeautifulSoup or None."""
    from bs4 import BeautifulSoup
    for parser in ('html5lib', 'lxml', 'html.parser'):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


class CentraleSupelecPresseCrawler(BaseCrawler):
    """Crawler for CentraleSupélec communiqués de presse."""

    site_id = "centralesupelec-fr-presse"
    site_name = "Custom: centralesupelec-fr-presse"
    base_url = "https://www.centralesupelec.fr"

    _LIST_URL = "https://www.centralesupelec.fr/presse/communiques"
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_WALL_MINUTES = 25

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, follow=False, timeout=30):
        """Fetch *url* via curl with up to 3 retries (backoff 1s/3s/9s).

        Returns (body_str, effective_url).  Both are None on complete failure.
        When *follow* is True curl follows redirects and appends a sentinel
        line so we can recover the final URL.
        """
        cmd = [
            'curl', '-sk', '--tls-max', '1.3', '--max-time', str(timeout),
            '-A', self.USER_AGENT,
        ]
        if follow:
            cmd += ['-L', '-w', '\n___EFF_URL___:%{url_effective}']
        cmd.append(url)

        for attempt in range(3):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
                body = r.stdout.decode('utf-8', errors='replace')
                if not body.strip():
                    raise ValueError("empty response")
                if follow:
                    parts = body.rsplit('\n___EFF_URL___:', 1)
                    text = parts[0] if len(parts) == 2 else body
                    eff_url = parts[1].strip() if len(parts) == 2 else url
                    return text, eff_url
                return body, url
            except Exception as exc:
                wait = [1, 3, 9][attempt]
                print(f"[{self.site_id}] curl error (attempt {attempt+1}/3) {url}: {exc}")
                if attempt < 2:
                    time.sleep(wait)

        return None, url

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, html):
        """Return list of dicts {node_id, title, pdf_url, published_date,
        raw_date, teaser} from a list-page HTML string."""
        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] BeautifulSoup construction failed: {exc}")
            return []

        if soup is None:
            return []

        items = []
        for article in soup.find_all('article', attrs={'data-history-node-id': True}):
            # Drupal also renders the catalogue's own article on every page,
            # including the empty boundary. Only teaser cards are publications.
            link_a = article.select_one('.wrap-teaser-content a.h5[href]')
            if link_a is None:
                continue
            node_id = (article.get('data-history-node-id') or '').strip()
            if not node_id:
                continue

            # Title
            title_span = article.find('span', class_='field--name-title')
            title = title_span.get_text(' ', strip=True) if title_span else ''
            if not title:
                link_a = article.find('a', class_='h5')
                title = link_a.get_text(' ', strip=True) if link_a else ''

            # The shared pdf_url column also preserves original non-PDF file
            # links. Keep Office URLs and filenames without labelling their
            # bytes as PDFs or claiming the PDF worker can process them.
            pdf_url = ''
            href = urljoin(self.base_url, link_a.get('href', ''))
            parsed_link = urlsplit(href)
            if (parsed_link.scheme in ('https', 'http') and parsed_link.netloc
                    and parsed_link.path.lower().endswith(('.pdf', '.docx', '.xlsx'))):
                pdf_url = href

            # Date
            date_div = article.find('div', class_='releasing-date')
            raw_date = date_div.get_text(strip=True) if date_div else ''
            published_date = _parse_fr_date(raw_date)

            # Teaser body (may be truncated — full text from detail page)
            body_div = article.find('div', class_='field--name-body')
            teaser = body_div.get_text(' ', strip=True) if body_div else ''

            items.append({
                'node_id': node_id,
                'title': title,
                'pdf_url': pdf_url,
                'published_date': published_date,
                'raw_date': raw_date,
                'teaser': teaser,
            })

        return items

    def _fetch_detail(self, node_id):
        """Follow /node/<id> redirect; return (full_abstract, page_url, pdf_url).

        All three may be empty/None on network failure.
        """
        node_url = f"{self.base_url}/node/{node_id}"
        html, eff_url = self._curl_get(node_url, follow=True)
        if not html:
            return '', node_url, None

        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] BS parse error for node {node_id}: {exc}")
            return '', eff_url, None

        if soup is None:
            return '', eff_url, None

        # Prefer the article scoped to this node_id; fall back to first article
        article = soup.find('article', attrs={'data-history-node-id': node_id})
        if not article:
            article = soup.find('article')

        full_text = ''
        detail_pdf_url = None

        if article:
            body_div = article.find('div', class_='field--name-body')
            if body_div:
                full_text = body_div.get_text(' ', strip=True)

            pdf_link = article.find('a', href=re.compile(r'\.pdf', re.I))
            if pdf_link:
                href = pdf_link.get('href', '')
                if href:
                    detail_pdf_url = href if href.startswith('http') else self.base_url + href

        # If redirect landed on the PDF itself, revert to node URL
        if eff_url and eff_url.lower().endswith('.pdf'):
            eff_url = node_url

        return full_text, eff_url, detail_pdf_url

    # ------------------------------------------------------------------
    # Pure enumeration entry points (crawl() and the parsers stay unchanged)
    # ------------------------------------------------------------------

    def _enumeration_items(self, html, url=None):
        """crawl()'s _parse_list_page; each row's detail URL is crawl()'s /node/<id>."""
        items = self._parse_list_page(html)
        return [dict(it, url=f"{self.base_url}/node/{it['node_id']}") for it in items]

    def _enumeration_detail(self, html, item, url=None):
        """crawl()'s per-item block with the /node/<id> page the framework already
        fetched (``url`` = final URL after redirects, as curl -L reported it)."""
        node_id = item['node_id']

        def fetched(_url, follow=False, timeout=30):
            return html, (url or _url)
        self._curl_get = fetched
        try:
            full_text, page_url, detail_pdf_url = self._fetch_detail(node_id)
        finally:
            del self._curl_get

        abstract = full_text.strip() if full_text else item['teaser'].strip()
        pdf_url = item['pdf_url'] or detail_pdf_url or None
        if not item['title'] or (len(abstract) < 50 and not pdf_url):
            return None

        original_filename = None
        if pdf_url:
            raw_name = urlsplit(pdf_url).path.rstrip('/').split('/')[-1]
            try:
                original_filename = unquote(raw_name)
            except Exception:
                original_filename = raw_name

        metadata = {'node_id': node_id, 'posted_date': item['raw_date']}
        if pdf_url and len(abstract) < 50:
            metadata['source_abstract_state'] = 'empty' if not abstract else 'short'
            metadata['source_fulltext_file_url'] = pdf_url
        if pdf_url and urlsplit(pdf_url).path.lower().endswith(('.docx', '.xlsx')):
            metadata['source_file_type'] = urlsplit(pdf_url).path.rsplit('.', 1)[-1].lower()
            metadata['source_file_conversion_pending'] = True
        return {
            'site_id': self.site_id,
            'external_id': node_id,
            'post_number': node_id,
            'title': item['title'],
            'abstract': abstract,
            'published_date': item['published_date'],
            'listed_date': item['published_date'],
            'url': page_url or f"{self.base_url}/node/{node_id}",
            'pdf_url': pdf_url,
            'original_filename': original_filename,
            'publisher': 'CentraleSupélec',
            'authors': None,
            'keywords': None,
            'category': 'Communiqué de presse',
            'doi': None,
            'department': None,
            'journal': None,
            'metadata': json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl list pages, fetch detail pages for full abstracts, save."""
        if limit is not None and limit <= 0:
            return 0
        self._check_cancelled()
        saved = 0
        seen_node_ids = set()
        start_time = time.time()
        limit_display = str(limit) if limit is not None else '∞'

        for page_num in range(self._MAX_PAGES):
            self._check_cancelled()
            # Wall-clock budget
            if (time.time() - start_time) / 60 >= self._MAX_WALL_MINUTES:
                print(f"[{self.site_id}] Wall-clock budget {self._MAX_WALL_MINUTES}min "
                      f"reached at page {page_num}. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page_num == self._MAX_PAGES - 1:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")

            # Log progress every 10 pages
            if page_num % 10 == 0:
                print(f"[{self.site_id}] page {page_num}: saved {saved}/{limit_display}")

            # Fetch list page
            list_url = f"{self._LIST_URL}?page={page_num}"
            try:
                html, _ = self._curl_get(list_url)
                self._check_cancelled()
            except KeyboardInterrupt:
                raise

            if not html:
                print(f"[{self.site_id}] Failed to fetch list page {page_num}. Stopping.")
                break

            # Parse
            try:
                items = self._parse_list_page(html)
            except Exception as exc:
                print(f"[{self.site_id}] Error parsing list page {page_num}: {exc}")
                items = []

            if not items:
                print(f"[{self.site_id}] No items on page {page_num}. Done.")
                break

            # Deduplicate publication nodes within this crawl.
            new_items = [it for it in items if it['node_id'] not in seen_node_ids]
            for it in new_items:
                self._check_cancelled()
                seen_node_ids.add(it['node_id'])

            if not new_items:
                print(f"[{self.site_id}] All items on page {page_num} already seen. Done.")
                break

            # Process items
            for it in new_items:
                if limit is not None and saved >= limit:
                    break

                node_id = it['node_id']
                try:
                    time.sleep(self._delay)
                    self._check_cancelled()

                    # Fetch detail page for full abstract + canonical URL
                    full_text, page_url, detail_pdf_url = self._fetch_detail(node_id)
                    self._check_cancelled()

                    abstract = full_text.strip() if full_text else it['teaser'].strip()
                    pdf_url = it['pdf_url'] or detail_pdf_url or None

                    if not it['title'] or (len(abstract) < 50 and not pdf_url):
                        self._report_source_failure('item_failed')
                        print(f"[{self.site_id}] Skipping node {node_id}: "
                              f"missing title or short abstract without a source file ({len(abstract)} chars)")
                        continue

                    # Extract original filename from PDF URL
                    original_filename = None
                    if pdf_url:
                        raw_name = urlsplit(pdf_url).path.rstrip('/').split('/')[-1]
                        try:
                            original_filename = unquote(raw_name)
                        except Exception:
                            original_filename = raw_name

                    metadata = {'node_id': node_id, 'posted_date': it['raw_date']}
                    if pdf_url and len(abstract) < 50:
                        metadata['source_abstract_state'] = 'empty' if not abstract else 'short'
                        metadata['source_fulltext_file_url'] = pdf_url
                    if pdf_url and urlsplit(pdf_url).path.lower().endswith(('.docx', '.xlsx')):
                        metadata['source_file_type'] = urlsplit(pdf_url).path.rsplit('.', 1)[-1].lower()
                        metadata['source_file_conversion_pending'] = True
                    paper = {
                        'site_id': self.site_id,
                        'external_id': node_id,
                        'post_number': node_id,
                        'title': it['title'],
                        'abstract': abstract,
                        'published_date': it['published_date'],
                        'listed_date': it['published_date'],
                        'url': page_url or f"{self.base_url}/node/{node_id}",
                        'pdf_url': pdf_url,
                        'original_filename': original_filename,
                        'publisher': 'CentraleSupélec',
                        'authors': None,
                        'keywords': None,
                        'category': 'Communiqué de presse',
                        'doi': None,
                        'department': None,
                        'journal': None,
                        'metadata': json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    if self._last_save_created:
                        saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_display}: "
                          f"{it['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {node_id} failed: {exc}")
                    continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
