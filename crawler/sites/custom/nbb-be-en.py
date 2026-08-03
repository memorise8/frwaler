# -*- coding: utf-8 -*-
"""Crawler for National Bank of Belgium (NBB) publications — English edition.

Target: https://www.nbb.be/en/publications-research/publications/all-publications?type[0]=709
Site: Drupal 10, paginated list (?page=N, 0-indexed), HTML scraping.
"""

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, '.')
from crawler.base_crawler import BaseCrawler


def _make_soup(html):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ('html5lib', 'lxml', 'html.parser'):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _parse_iso_date(dt_str):
    """'2026-04-17T07:45:00Z' → '2026-04-17', or None."""
    if not dt_str:
        return None
    m = re.match(r'(\d{4}-\d{2}-\d{2})', dt_str.strip())
    return m.group(1) if m else None


def _last_path_segment(url):
    """'/doc/.../foo.pdf' → 'foo.pdf', or None."""
    if not url:
        return None
    seg = url.rstrip('/').split('/')[-1].split('?')[0].split('#')[0]
    return seg if seg and '.' in seg else None


class NbbBeEnCrawler(BaseCrawler):
    """Publications crawler for www.nbb.be (National Bank of Belgium, English).

    Targets publication type 709 (Corporate Report and similar) at
    /en/publications-research/publications/all-publications?type[0]=709.
    """

    site_id = "nbb-be-en"
    site_name = "Custom: nbb-be-en"
    base_url = "https://www.nbb.be"

    _LIST_BASE = "https://www.nbb.be/en/publications-research/publications/all-publications"
    _LIST_PARAMS = "after_date=&before_date=&issue_number=&quick_search=&type%5B0%5D=709"
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _RATE_SLEEP = 1.0
    _SKIP_ABSTRACT_CHARS = 50  # skip (don't save) items with abstract shorter than this

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, retries=3):
        """Fetch URL with curl; return decoded text or None on failure."""
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    [
                        'curl', '--tls-max', '1.3', '-sk',
                        '--max-time', '30',
                        '-A', self.USER_AGENT,
                        '-H', 'Accept-Language: en-US,en;q=0.9',
                        '-H', 'Accept: text/html,application/xhtml+xml,*/*;q=0.8',
                        url,
                    ],
                    capture_output=True,
                    timeout=40,
                )
                text = result.stdout.decode('utf-8', errors='replace')
                if text.strip():
                    return text
                if attempt < retries - 1:
                    wait = 3 ** attempt
                    print(f"[nbb-be-en] empty response, retrying in {wait}s ({url})")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < retries - 1:
                    wait = 3 ** attempt
                    print(f"[nbb-be-en] curl error: {exc}, retrying in {wait}s")
                    time.sleep(wait)
                else:
                    print(f"[nbb-be-en] curl failed after {retries} attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_list_page(self, html):
        """Return list of dicts: href, title, pub_type, listed_date."""
        soup = _make_soup(html)
        if soup is None:
            return []
        items = []
        for row in soup.select('.views-row'):
            try:
                a = row.find('a', class_='node-publication-teaser')
                if not a:
                    # fallback: any anchor inside the row
                    a = row.find('a', href=True)
                if not a:
                    continue
                href = a.get('href', '')
                if not href.startswith('/'):
                    continue

                # Title
                label = row.select_one('.node-publication-teaser__label span')
                if not label:
                    label = row.select_one('.node-publication-teaser__label')
                title = label.get_text(' ', strip=True) if label else ''
                if not title:
                    continue

                # Publication type
                type_el = row.select_one('.node-publication-teaser__type')
                pub_type = ''
                if type_el:
                    # strip any SVG children
                    for svg in type_el.find_all('svg'):
                        svg.decompose()
                    pub_type = type_el.get_text(' ', strip=True)

                # Date from list
                time_el = row.select_one('time[datetime]')
                listed_date = None
                if time_el:
                    listed_date = _parse_iso_date(time_el.get('datetime', ''))

                items.append({
                    'href': href,
                    'title': title,
                    'pub_type': pub_type,
                    'listed_date': listed_date,
                })
            except Exception as exc:
                print(f"[nbb-be-en] list item parse error: {exc}")
                continue
        return items

    def _parse_detail_page(self, html, detail_url):
        """Return dict: title, abstract, published_date, keywords, pdf_url,
        original_filename, node_id, pub_type, authors."""
        soup = _make_soup(html)
        if soup is None:
            return {}

        result = {}

        # Title
        h1 = soup.select_one('h1')
        result['title'] = h1.get_text(' ', strip=True) if h1 else ''

        # Node ID from Drupal settings JSON
        node_id = None
        settings_el = soup.select_one('[data-drupal-selector="drupal-settings-json"]')
        if settings_el:
            try:
                raw = settings_el.string or settings_el.get_text()
                data = json.loads(raw)
                current_path = data.get('path', {}).get('currentPath', '')
                m = re.search(r'node/(\d+)', current_path)
                if m:
                    node_id = m.group(1)
            except Exception:
                pass
        if not node_id:
            node_id = detail_url.rstrip('/').split('/')[-1]
        result['node_id'] = node_id

        # Publication date from detail header
        top = soup.select_one('.node-publication-full-top__top')
        published_date = None
        if top:
            time_el = top.select_one('time[datetime]')
            if time_el:
                published_date = _parse_iso_date(time_el.get('datetime', ''))
        if not published_date:
            time_el = soup.select_one('time[datetime]')
            if time_el:
                published_date = _parse_iso_date(time_el.get('datetime', ''))
        result['published_date'] = published_date

        # Abstract — intro block
        abstract = ''
        for sel in (
            '.node-publication-full-top__intro',
            '.node-news-article-full-top__intro',
            '.field--name-field-introduction',
            '.field--field-introduction',
        ):
            el = soup.select_one(sel)
            if el:
                abstract = el.get_text(' ', strip=True)
                if abstract:
                    break

        # If still short, also pull from body / main text blocks
        if len(abstract) < self._SKIP_ABSTRACT_CHARS:
            for sel in (
                '.field--name-body',
                '.node-publication-full-body',
                'article .text',
            ):
                body_el = soup.select_one(sel)
                if body_el:
                    body_text = body_el.get_text(' ', strip=True)
                    if body_text:
                        abstract = (abstract + ' ' + body_text).strip()
                    break

        result['abstract'] = abstract

        # Keywords — links inside the keywords value div
        kw_div = soup.select_one('.node-publication-full-top__value--keywords')
        keywords = []
        if kw_div:
            for a in kw_div.select('a'):
                kw = a.get_text(strip=True)
                if kw:
                    keywords.append(kw)
        result['keywords'] = ', '.join(keywords)

        # Publication type
        type_div = soup.select_one('.node-publication-full-top__type')
        pub_type = ''
        if type_div:
            for svg in type_div.find_all('svg'):
                svg.decompose()
            pub_type = type_div.get_text(' ', strip=True)
        result['pub_type'] = pub_type

        # Authors — look for author/contributor field
        authors = ''
        for sel in (
            '.field--name-field-authors',
            '.field--name-field-author',
            '.node-publication-full-top__authors',
        ):
            el = soup.select_one(sel)
            if el:
                parts = [a.get_text(strip=True) for a in el.select('a')]
                if not parts:
                    parts = [el.get_text(' ', strip=True)]
                authors = '; '.join(p for p in parts if p)
                break
        result['authors'] = authors

        # PDF URL — prefer "Complete version" (btn-primary) → first any .pdf link
        pdf_url = None
        for a in soup.select('a.btn-primary'):
            href = a.get('href', '')
            if href.lower().endswith('.pdf'):
                pdf_url = href if href.startswith('http') else self.base_url + href
                break
        if not pdf_url:
            for a in soup.select('a[href]'):
                href = a.get('href', '')
                if href.lower().endswith('.pdf'):
                    pdf_url = href if href.startswith('http') else self.base_url + href
                    break
        result['pdf_url'] = pdf_url
        result['original_filename'] = _last_path_segment(pdf_url) if pdf_url else None

        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl NBB publications and save to DB. Returns number of saved items."""
        saved = 0
        seen_urls = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else 'inf'

        for page in range(self._MAX_PAGES):
            # Wall-clock budget check
            if time.time() - start_time > self._WALL_SECONDS:
                print(f"[nbb-be-en] wall-clock budget reached at page {page}, stopping")
                break

            # Limit check
            if limit is not None and saved >= limit:
                break

            # Progress log every 10 pages
            if page > 0 and page % 10 == 0:
                print(f"[nbb-be-en] page {page}: saved {saved}/{limit_str}")

            # Build URL (page 0 omits &page= for cleanliness)
            if page == 0:
                list_url = f"{self._LIST_BASE}?{self._LIST_PARAMS}"
            else:
                list_url = f"{self._LIST_BASE}?{self._LIST_PARAMS}&page={page}"

            # Fetch list page with retry
            html = self._curl_get(list_url)
            if not html:
                print(f"[nbb-be-en] page {page}: failed to fetch list, stopping")
                break

            items = self._parse_list_page(html)
            if not items:
                print(f"[nbb-be-en] page {page}: no items found, stopping")
                break

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                detail_url = self.base_url + item['href']
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                # Per-item isolation — one bad page must NOT abort the whole crawl
                try:
                    time.sleep(self._RATE_SLEEP)

                    detail_html = self._curl_get(detail_url)
                    if not detail_html:
                        print(f"[nbb-be-en] detail fetch failed: {detail_url}")
                        continue

                    detail = self._parse_detail_page(detail_html, detail_url)

                    title = detail.get('title') or item['title']
                    abstract = detail.get('abstract', '')

                    if len(abstract) < self._SKIP_ABSTRACT_CHARS:
                        print(
                            f"[nbb-be-en] skip (abstract {len(abstract)} chars): {title[:60]}"
                        )
                        continue

                    node_id = detail.get('node_id')
                    url_slug = item['href'].rstrip('/').split('/')[-1]
                    # external_id = slug (stable, unique per publication)
                    external_id = url_slug

                    published_date = detail.get('published_date') or item['listed_date']
                    listed_date = item['listed_date']
                    pdf_url = detail.get('pdf_url') or ''
                    original_filename = detail.get('original_filename')
                    keywords = detail.get('keywords', '')
                    pub_type = detail.get('pub_type') or item.get('pub_type', '')
                    authors = detail.get('authors', '')

                    metadata = {
                        'posted_date': listed_date,
                        'node_id': node_id,
                        'pub_type': pub_type,
                        'slug': url_slug,
                    }
                    if original_filename:
                        metadata['originalFilename'] = original_filename

                    paper = {
                        'site_id': self.site_id,
                        'external_id': external_id,
                        'post_number': node_id,
                        'title': title,
                        'abstract': abstract,
                        'published_date': published_date,
                        'posted_date': listed_date,
                        'url': detail_url,
                        'pdf_url': pdf_url or None,
                        'original_filename': original_filename,
                        'category': pub_type,
                        'publisher': 'National Bank of Belgium',
                        'authors': authors or None,
                        'keywords': keywords or None,
                        'metadata': json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[nbb-be-en] saved [{saved}/{limit_str}]: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[nbb-be-en] item failed ({detail_url}): {exc}")
                    continue

            # Dedup / loop guard
            if new_on_page == 0:
                print(f"[nbb-be-en] page {page}: all items already seen, stopping")
                break

        print(f"[nbb-be-en] crawl complete: saved {saved} items")
        return saved
