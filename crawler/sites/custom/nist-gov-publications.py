# -*- coding: utf-8 -*-
"""NIST Publications crawler — https://www.nist.gov/publications/search"""

import json
import re
import subprocess
import time

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from crawler.base_crawler import BaseCrawler

_BS4_PARSERS = ['html5lib', 'lxml', 'html.parser']


def _make_soup(html):
    for parser in _BS4_PARSERS:
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


class NISTPublicationsCrawler(BaseCrawler):

    site_id = "nist-gov-publications"
    site_name = "Custom: nist-gov-publications"
    base_url = "https://www.nist.gov"

    _SEARCH_URL = "https://www.nist.gov/publications/search"

    def _curl_get(self, url, retries=3):
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            url,
        ]
        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                raw = result.stdout
                if raw and len(raw) > 500:
                    try:
                        return raw.decode('utf-8', errors='replace')
                    except Exception:
                        return raw.decode('latin-1', errors='replace')
                if attempt < retries - 1:
                    wait = [1, 3, 9][attempt]
                    print(f"[nist-gov-publications] Empty/short response from {url}, retrying in {wait}s...")
                    time.sleep(wait)
            except Exception as e:
                if attempt < retries - 1:
                    wait = [1, 3, 9][attempt]
                    print(f"[nist-gov-publications] curl error: {e}, retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"[nist-gov-publications] curl failed after {retries} attempts for {url}: {e}")
        return None

    def _parse_list_page(self, html):
        links = re.findall(r'href="(/publications/[a-z0-9][a-z0-9\-]+)"', html)
        seen = set()
        unique = []
        for link in links:
            if link not in seen:
                seen.add(link)
                unique.append(self.base_url + link)
        return unique

    def _get_nist_field(self, soup, label_text):
        """Extract value from a nist-field div by its label text."""
        for div in soup.find_all('div', class_=lambda c: c and 'nist-field' in ' '.join(c)):
            label_div = div.find('div', class_='nist-field__label')
            if label_div and label_text.lower() in label_div.get_text(strip=True).lower():
                # Remove the label portion from full text
                full_text = div.get_text(' ', strip=True)
                label_val = label_div.get_text(strip=True)
                value = full_text[len(label_val):].strip()
                return value
        return ''

    def _parse_detail_page(self, html, url):
        try:
            soup = _make_soup(html)
        except Exception as e:
            print(f"[nist-gov-publications] BeautifulSoup parse error for {url}: {e}")
            return None

        if not soup:
            return None

        # --- Meta tags ---
        metas = {}
        authors = []
        try:
            for meta in soup.find_all('meta'):
                name = meta.get('name') or meta.get('property', '')
                content = meta.get('content', '')
                if not name or not content:
                    continue
                if name == 'citation_author':
                    authors.append(content.strip())
                elif name not in metas:
                    metas[name] = content
        except Exception:
            pass

        # Title
        title = ''
        try:
            title = (metas.get('citation_title') or metas.get('og:title', '')).strip()
            if not title:
                h1 = soup.find('h1')
                title = h1.get_text(strip=True) if h1 else ''
        except Exception:
            pass

        if not title:
            return None

        # Published date
        pub_date = ''
        try:
            raw_date = metas.get('citation_publication_date', '') or metas.get('article:published_time', '')
            m = re.match(r'(\d{4}-\d{2}-\d{2})', raw_date)
            if m:
                pub_date = m.group(1)
        except Exception:
            pass

        # PDF URL
        pdf_url = metas.get('citation_pdf_url', '').strip()

        # --- Abstract from body div ---
        abstract = ''
        try:
            abs_div = soup.find('div', class_='text-with-summary')
            if abs_div:
                # Remove h3/h4 "Abstract" heading inside the div
                for heading in abs_div.find_all(['h1', 'h2', 'h3', 'h4']):
                    heading.decompose()
                abstract = abs_div.get_text(' ', strip=True)
        except Exception:
            pass

        # Fallback to meta description
        if not abstract or len(abstract) < 50:
            abstract = (metas.get('description') or metas.get('og:description', '')).strip()

        if not abstract or len(abstract) < 50:
            print(f"[nist-gov-publications] Abstract too short for {url} ({len(abstract)} chars), skipping.")
            return None

        # --- Structured fields ---
        keywords_list = []
        try:
            kw_raw = self._get_nist_field(soup, 'Keywords')
            if kw_raw:
                keywords_list = [k.strip() for k in kw_raw.split(',') if k.strip()]
        except Exception:
            pass

        pub_type = ''
        try:
            pub_type = self._get_nist_field(soup, 'Pub Type').strip()
        except Exception:
            pass

        # DOI from links
        doi = ''
        try:
            for a in soup.find_all('a', href=True):
                href = a['href']
                if 'doi.org/10.' in href:
                    doi = 'https://doi.org/' + href.split('doi.org/')[-1].strip().rstrip('/')
                    break
        except Exception:
            pass

        # External ID from shortlink node ID
        external_id = ''
        try:
            shortlink = soup.find('link', rel='shortlink')
            if shortlink:
                m = re.search(r'/node/(\d+)', shortlink.get('href', ''))
                if m:
                    external_id = m.group(1)
        except Exception:
            pass
        if not external_id:
            m = re.search(r'/publications/([a-z0-9\-]+)$', url)
            if m:
                external_id = m.group(1)

        meta_obj = {'pub_type': pub_type}
        if metas.get('citation_volume'):
            meta_obj['volume'] = metas['citation_volume']

        return {
            'external_id': external_id,
            'title': title,
            'authors': json.dumps(authors, ensure_ascii=False),
            'abstract': abstract,
            'keywords': json.dumps(keywords_list, ensure_ascii=False),
            'published_date': pub_date,
            'url': url,
            'pdf_url': pdf_url,
            'doi': doi,
            'category': pub_type,
            'department': '',
            'metadata': json.dumps(meta_obj, ensure_ascii=False),
        }

    def crawl(self, limit=None):
        start_time = time.time()
        MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
        MAX_PAGES = 200

        saved = 0
        seen_urls = set()
        limit_str = str(limit) if limit is not None else 'inf'

        print(f"[nist-gov-publications] Starting crawl (limit={limit_str})")

        for page_num in range(MAX_PAGES):
            # Wall-clock budget
            if time.time() - start_time > MAX_WALL_SECONDS:
                print(f"[nist-gov-publications] Wall-clock budget reached at page {page_num}, stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page_num % 10 == 0:
                print(f"[nist-gov-publications] page {page_num}: saved {saved}/{limit_str}")

            list_url = f"{self._SEARCH_URL}?page={page_num}"
            html = self._curl_get(list_url)
            if not html:
                print(f"[nist-gov-publications] Failed to fetch list page {page_num}, stopping.")
                break

            pub_urls = self._parse_list_page(html)
            if not pub_urls:
                print(f"[nist-gov-publications] No publications on page {page_num}, end of results.")
                break

            new_urls = [u for u in pub_urls if u not in seen_urls]
            if not new_urls:
                print(f"[nist-gov-publications] All URLs on page {page_num} already seen, stopping.")
                break

            for pub_url in new_urls:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > MAX_WALL_SECONDS:
                    print(f"[nist-gov-publications] Wall-clock budget reached, stopping mid-page.")
                    break

                seen_urls.add(pub_url)

                try:
                    time.sleep(self._delay)
                    detail_html = self._curl_get(pub_url)
                    if not detail_html:
                        print(f"[nist-gov-publications] Failed to fetch {pub_url}, skipping.")
                        continue

                    paper = self._parse_detail_page(detail_html, pub_url)
                    if paper is None:
                        continue

                    paper['site_id'] = self.site_id
                    self._save_paper(paper)
                    saved += 1
                    print(f"[nist-gov-publications] Saved [{saved}]: {paper['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[nist-gov-publications] item {pub_url} failed: {exc}")
                    continue

            if page_num == MAX_PAGES - 1:
                print(f"[nist-gov-publications] Safety cap of {MAX_PAGES} pages reached, stopping.")

        print(f"[nist-gov-publications] Done. Saved {saved} records.")
        return saved
