# -*- coding: utf-8 -*-
"""Crawler for ISS National Laboratory Annual/Quarterly Reports and Metrics."""

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler


def _make_soup(raw):
    """Parse HTML with fallback: html5lib → lxml → html.parser."""
    if isinstance(raw, (bytes, bytearray)):
        text = raw.decode('utf-8', errors='replace')
    else:
        text = str(raw) if not isinstance(raw, str) else raw
    for parser in ('html5lib', 'lxml', 'html.parser'):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(text, parser)
        except Exception:
            continue
    return None


class ISSNationalLabAboutCrawler(BaseCrawler):
    """Crawler for ISS National Lab Annual/Quarterly Reports and Metrics page."""

    site_id = "issnationallab-org-about"
    site_name = "Custom: issnationallab-org-about"
    base_url = "https://issnationallab.org"

    _LIST_URL = "https://issnationallab.org/about/annual-quarterly-reports-metrics/"

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, retries=3):
        """Fetch URL via curl with retry/exponential-backoff. Returns text or None."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "-L",
            "--max-time", "30",
            "-H", "Accept: text/html,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            url,
        ]
        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                if result.stdout:
                    return result.stdout.decode('utf-8', errors='replace')
                if attempt < retries - 1:
                    wait = (attempt + 1) * 3
                    print(f"[{self.site_id}] Empty response for {url[:80]}, "
                          f"retry in {wait}s...")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < retries - 1:
                    wait = (attempt + 1) * 3
                    print(f"[{self.site_id}] curl error: {exc}, retry in {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after {retries} attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_external_id(href, year):
        """Derive a stable external_id from an annual report URL."""
        m = re.search(r'/(fy\d+[a-z0-9-]+)/', href.lower())
        if m:
            return re.sub(r'[^a-z0-9-]', '-', m.group(1)).strip('-')
        return f'ar{year}'

    @staticmethod
    def _quarter_date(fy_year, q_num):
        """Approximate end-date for a US fiscal year quarter (FY starts Oct 1)."""
        cal_year = 2000 + fy_year
        return {
            1: f'{cal_year - 1}-12-31',
            2: f'{cal_year}-03-31',
            3: f'{cal_year}-06-30',
            4: f'{cal_year}-09-30',
        }.get(q_num, f'{cal_year}-09-30')

    def _extract_detail_abstract(self, html):
        """Extract introductory paragraphs from an annual report detail page."""
        soup = _make_soup(html)
        if not soup:
            return ''
        pc = (soup.find(class_='page-content')
              or soup.find('main')
              or soup.find('article'))
        if not pc:
            return ''
        parts = []
        for p in pc.find_all('p'):
            txt = p.get_text(separator=' ', strip=True)
            if len(txt) < 40:
                continue
            # Skip navigation text
            if re.match(r'^[«»]|^Back to', txt):
                continue
            parts.append(txt)
            if len('\n\n'.join(parts)) > 2000:
                break
        return '\n\n'.join(parts)

    def _parse_listing(self, raw):
        """Parse the main listing page; return list of report entry dicts."""
        soup = _make_soup(raw)
        entries = []
        if not soup:
            return entries

        pc = soup.find(class_='page-content') or soup

        # ---------------------------------------------------------------
        # Modern sections (2021-2026): each year has an h2 inside its own
        # innermost wpb_wrapper, followed by a description paragraph and
        # links to the annual report and quarterly PDFs.
        # ---------------------------------------------------------------
        for h2 in pc.find_all('h2'):
            year_m = re.search(r'\b(20\d\d)\b', h2.get_text())
            if not year_m:
                continue
            year = int(year_m.group(1))
            if year < 2021:
                continue

            # h2.parent is the innermost wpb_wrapper that contains only
            # this year's heading, description, and links.
            container = h2.parent

            # First substantial paragraph that is not a link list
            year_desc = ''
            for p in container.find_all('p', recursive=False):
                txt = p.get_text(separator=' ', strip=True)
                if len(txt) > 100 and not re.match(r'^(FY\d|View |Download )', txt):
                    year_desc = txt
                    break

            annual_entry = None
            quarterly_entries = []

            for a in container.find_all('a', href=True):
                href = a['href'].strip()
                title = a.get_text(strip=True)
                if not href or not title:
                    continue
                if href.startswith('/'):
                    href = self.base_url + href

                if '/download/' in href:
                    q_m = re.search(r'Q(\d)', title, re.I)
                    fy_m = re.search(r'FY(\d+)', title, re.I)
                    q_num = int(q_m.group(1)) if q_m else 0
                    fy = int(fy_m.group(1)) if fy_m else year - 2000
                    ext_id = (f'fy{fy:02d}-q{q_num}' if q_num
                              else f'fy{fy:02d}-dl-{len(quarterly_entries)}')
                    quarterly_entries.append({
                        'kind': 'quarterly',
                        'year': year,
                        'title': title,
                        'url': href,
                        'pdf_url': href,
                        'external_id': ext_id,
                        'published_date': self._quarter_date(fy, q_num),
                        'category': 'Quarterly Report',
                        'year_description': year_desc,
                    })
                elif (not annual_entry
                      and any(kw in href.lower()
                              for kw in ('annual-report', 'ar20', '/fy'))):
                    eid = self._derive_external_id(href, year)
                    annual_entry = {
                        'kind': 'annual_detail',
                        'year': year,
                        'title': f'{year} ISS National Lab Annual Report',
                        'url': href,
                        'detail_url': href,
                        'pdf_url': '',
                        'external_id': eid,
                        'published_date': f'{year}-09-30',
                        'category': 'Annual Report',
                        'year_description': year_desc,
                    }

            if annual_entry:
                entries.append(annual_entry)
            entries.extend(quarterly_entries)

        # ---------------------------------------------------------------
        # Old sections (2012-2020): section[id='arXXXX'] blocks where the
        # download URL lives in a data-url attribute on a .link-block div.
        # ---------------------------------------------------------------
        for sec in pc.find_all('section', id=True):
            sid = sec.get('id', '')
            if not re.match(r'^ar\d{4}$', sid):
                continue
            year = int(sid[2:])

            year_desc = ''
            for p in sec.find_all('p'):
                txt = p.get_text(separator=' ', strip=True)
                if len(txt) > 50:
                    year_desc = txt
                    break

            lb = sec.find(class_='link-block')
            pdf_url = ''
            if lb:
                du = lb.get('data-url', '')
                if du and '/download/' in du:
                    pdf_url = du.split('?')[0]  # strip tmstv cache-buster

            url = pdf_url if pdf_url else f'{self._LIST_URL}#{sid}'

            entries.append({
                'kind': 'annual_pdf',
                'year': year,
                'title': f'{year} ISS National Lab Annual Report',
                'url': url,
                'detail_url': None,
                'pdf_url': pdf_url,
                'external_id': f'ar{year}',
                'published_date': f'{year}-09-30',
                'category': 'Annual Report',
                'year_description': year_desc,
            })

        return entries

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl annual/quarterly reports from the ISS National Lab about page.

        Parameters
        ----------
        limit:
            Maximum number of records to save. None means unlimited.
        """
        start_time = time.time()
        MAX_WALL = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute hard budget

        print(f"[{self.site_id}] Fetching listing page: {self._LIST_URL}")
        raw = self._curl_get(self._LIST_URL)
        if not raw:
            print(f"[{self.site_id}] Failed to fetch listing page")
            return 0

        entries = self._parse_listing(raw)
        total = len(entries)
        print(f"[{self.site_id}] Found {total} report entries on listing page")

        saved = 0
        seen_urls = set()
        limit_str = str(limit) if limit is not None else 'inf'
        # This site has a single listing page; p tracks logical progress
        p_num = 1

        for i, entry in enumerate(entries):
            # Limit guard
            if limit is not None and saved >= limit:
                break

            # Wall-clock budget
            if time.time() - start_time > MAX_WALL:
                print(f"[{self.site_id}] Wall-clock budget (25 min) exceeded, "
                      f"stopping cleanly at entry {i}")
                break

            url = entry.get('url', '')
            if not url:
                continue

            # URL-based deduplication prevents looping on identical entries
            if url in seen_urls:
                print(f"[{self.site_id}] Duplicate URL, skipping: {url[:80]}")
                continue
            seen_urls.add(url)

            # Progress log every 10 items
            if i > 0 and i % 10 == 0:
                print(f"[{self.site_id}] page {p_num}: saved {saved}/{limit_str}")

            try:
                # Base abstract: title + year description from listing page
                abstract = (f"{entry['title']}\n\n"
                            f"{entry.get('year_description', '')}").strip()

                # For annual reports with web detail pages, fetch richer content
                if entry.get('kind') == 'annual_detail' and entry.get('detail_url'):
                    time.sleep(self._delay)
                    detail_html = self._curl_get(entry['detail_url'])
                    if detail_html:
                        rich = self._extract_detail_abstract(detail_html)
                        if rich and len(rich) >= 100:
                            abstract = rich

                # Skip items whose abstract is too short to be useful
                if len(abstract) < 50:
                    print(f"[{self.site_id}] Abstract <50 chars for "
                          f"'{entry['title'][:60]}', skipping")
                    continue

                paper = {
                    'id': None,
                    'site_id': self.site_id,
                    'external_id': entry.get('external_id', ''),
                    'title': entry.get('title', ''),
                    'authors': json.dumps([], ensure_ascii=False),
                    'abstract': abstract,
                    'category': entry.get('category', ''),
                    'keywords': json.dumps([], ensure_ascii=False),
                    'published_date': entry.get('published_date', ''),
                    'url': url,
                    'pdf_url': entry.get('pdf_url', '') or '',
                    'doi': '',
                    'department': '',
                    'metadata': json.dumps({
                        'year': entry.get('year'),
                        'kind': entry.get('kind'),
                    }, ensure_ascii=False),
                }

                self._save_paper(paper)
                saved += 1
                print(f"[{self.site_id}] Saved {saved}/{limit_str}: "
                      f"{entry['title'][:60]}")

                # Rate-limit between non-detail items
                if entry.get('kind') != 'annual_detail':
                    time.sleep(self._delay)

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {i} failed: {exc}")
                continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
