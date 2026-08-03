# -*- coding: utf-8 -*-
"""NOAA Institutional Repository crawler — https://repository.library.noaa.gov/view/noaa/"""

import json
import re
import subprocess
import sys
import os
import time

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


class NOAARepositoryViewCrawler(BaseCrawler):

    site_id = "repository-library-noaa-gov-view"
    site_name = "Custom: repository-library-noaa-gov-view"
    base_url = "https://repository.library.noaa.gov"

    _BROWSE_URL = "https://repository.library.noaa.gov/browse/recent"
    _DETAIL_BASE = "https://repository.library.noaa.gov/view/noaa/"

    def _curl_get(self, url, retries=3):
        # NOTE: No custom headers — Akamai WAF blocks requests with custom User-Agent.
        # Plain 'curl -sk' passes with the default curl UA.
        cmd = ["curl", "-sk", "--max-time", "30", url]
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
                    print(f"[{self.site_id}] Short/empty response for {url}, retry in {wait}s")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < retries - 1:
                    wait = [1, 3, 9][attempt]
                    print(f"[{self.site_id}] curl error: {exc}, retry in {wait}s")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed for {url}: {exc}")
        return None

    def _is_not_found(self, html):
        """Return True if the response is a 404/not-found page."""
        if not html:
            return True
        # Drupal 404: <title>Page not found</title>
        title_match = re.search(r'<title>(.*?)</title>', html, re.I)
        if title_match and 'not found' in title_match.group(1).lower():
            return True
        if 'Access Denied' in html[:600]:
            return True
        return False

    def _parse_list_ids(self, html):
        """Extract numeric NOAA IDs from a browse/recent page."""
        ids = re.findall(r'href="/view/noaa/(\d+)"', html)
        seen = set()
        result = []
        for nid in ids:
            if nid not in seen:
                seen.add(nid)
                result.append(nid)
        return result

    def _parse_detail(self, html, noaa_id):
        """Parse citation_* meta tags from a detail page. Returns dict or None."""
        from html import unescape
        from urllib.parse import unquote

        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] soup parse error for {noaa_id}: {exc}")
            soup = None

        def get_meta_all(name):
            if soup:
                tags = soup.find_all('meta', attrs={'name': name})
                return [t.get('content', '') for t in tags if t.get('content')]
            return re.findall(
                rf'<meta\s+name="{re.escape(name)}"\s+content="([^"]*)"', html
            )

        def get_meta(name):
            vals = get_meta_all(name)
            return vals[0] if vals else None

        title = get_meta('citation_title')
        if title:
            title = unescape(title).strip()
        if not title:
            return None

        # Authors — semicolon-joined
        authors_raw = get_meta_all('citation_author')
        authors = '; '.join(a.strip() for a in authors_raw if a.strip()) or None

        # Keywords — comma-joined
        kw_raw = get_meta_all('citation_keywords')
        keywords = ', '.join(k.strip() for k in kw_raw if k.strip()) or None

        # Published date — usually year-only ("2026") or full ISO
        pub_date_raw = get_meta('citation_publication_date')
        published_date = None
        if pub_date_raw:
            pub_date_raw = pub_date_raw.strip()
            if re.match(r'^\d{4}$', pub_date_raw):
                published_date = pub_date_raw
            elif re.match(r'^\d{4}/\d{2}/\d{2}$', pub_date_raw):
                published_date = pub_date_raw.replace('/', '-')
            elif re.match(r'^\d{4}-\d{2}-\d{2}$', pub_date_raw):
                published_date = pub_date_raw
            else:
                published_date = pub_date_raw

        # Publisher — strip ror.org / URL suffixes, semicolon-join multiple
        publishers_raw = get_meta_all('citation_publisher')
        publishers_clean = []
        for p in publishers_raw:
            p = p.split('|')[0].split(' - https://')[0].strip()
            if p:
                publishers_clean.append(p)
        publisher = '; '.join(publishers_clean) or None

        # PDF URL
        pdf_url = get_meta('citation_pdf_url') or None

        # Abstract
        abstract_raw = get_meta('citation_abstract')
        abstract = unescape(abstract_raw).strip() if abstract_raw else None

        # DOI
        doi_raw = get_meta('citation_doi')
        doi = unquote(doi_raw.strip()) if doi_raw else None

        # Canonical URL (always use HTTPS)
        url = f"https://repository.library.noaa.gov/view/noaa/{noaa_id}"

        # original_filename from pdf_url path
        original_filename = None
        if pdf_url:
            fname = pdf_url.rstrip('/').split('/')[-1]
            if fname and '.' in fname:
                original_filename = fname

        metadata = {
            'noaa_id': noaa_id,
            'citation_publication_date_raw': pub_date_raw,
            'publishers_raw': publishers_raw,
        }
        if doi:
            metadata['doi'] = doi

        return {
            'external_id': noaa_id,
            'post_number': noaa_id,
            'title': title,
            'abstract': abstract,
            'authors': authors,
            'keywords': keywords,
            'published_date': published_date,
            'listed_date': None,
            'publisher': publisher,
            'url': url,
            'pdf_url': pdf_url,
            'doi': doi,
            'original_filename': original_filename,
            'category': None,
            'department': None,
            'journal': None,
            'metadata': json.dumps(metadata, ensure_ascii=False),
        }

    def _process_item(self, noaa_id, seen_urls):
        """Fetch and save one item. Returns True if saved, False otherwise."""
        item_url = f"{self._DETAIL_BASE}{noaa_id}"
        if item_url in seen_urls:
            return False
        seen_urls.add(item_url)

        time.sleep(self._delay)
        detail_html = self._curl_get(item_url)
        if not detail_html or self._is_not_found(detail_html):
            return False

        paper = self._parse_detail(detail_html, noaa_id)
        if not paper:
            return False

        abstract = paper.get('abstract') or ''
        if len(abstract) < 50:
            print(f"[{self.site_id}] Abstract too short (<50 chars) for {item_url}, skipping")
            return False

        self._save_paper(paper)
        return True

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        start_time = time.time()
        limit_or_inf = limit if limit is not None else float('inf')

        # ---- Phase 1: browse/recent pages (newest first, up to ~268 items) ----
        rows = 20
        browse_page = 0
        browse_done = False
        min_browse_id = None

        while not browse_done and saved < limit_or_inf:
            if time.time() - start_time > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[{self.site_id}] 25-minute budget reached, stopping")
                return saved

            offset = browse_page * rows
            list_url = (
                f"{self._BROWSE_URL}?start={offset}&rows={rows}"
                "&sortBy=fgs.createdDate+desc"
            )
            html = self._curl_get(list_url)
            if not html or self._is_not_found(html):
                print(f"[{self.site_id}] List page failed at offset {offset}, ending browse phase")
                browse_done = True
                break

            ids = self._parse_list_ids(html)
            if not ids:
                browse_done = True
                break

            for noaa_id in ids:
                if saved >= limit_or_inf:
                    return saved
                try:
                    if self._process_item(noaa_id, seen_urls):
                        saved += 1
                        if min_browse_id is None or int(noaa_id) < int(min_browse_id):
                            min_browse_id = noaa_id
                    else:
                        # Still track min ID even if not saved (abstract too short, etc.)
                        if min_browse_id is None or int(noaa_id) < int(min_browse_id):
                            min_browse_id = noaa_id
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {noaa_id} failed: {exc}")
                    continue

            browse_page += 1
            if browse_page % 10 == 0:
                print(f"[{self.site_id}] page {browse_page}: saved {saved}/{limit_or_inf}")

            # browse/recent caps at 268 items (14 pages of 20)
            if offset + rows >= 270:
                browse_done = True

        # ---- Phase 2: sequential ID iteration backward for limit > 268 ----
        if saved >= limit_or_inf:
            return saved

        # Start just below the lowest ID seen in browse/recent
        if min_browse_id is None:
            min_browse_id = "72459"

        current_id = int(min_browse_id) - 1
        seq_items = 0
        consecutive_misses = 0

        while saved < limit_or_inf and current_id > 0:
            if time.time() - start_time > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[{self.site_id}] 25-minute budget reached, stopping")
                break

            if consecutive_misses >= 50:
                print(f"[{self.site_id}] 50 consecutive misses at id={current_id}, stopping")
                break

            seq_items += 1
            if seq_items % 10 == 0:
                print(
                    f"[{self.site_id}] seq item {seq_items}: "
                    f"id={current_id} saved {saved}/{limit_or_inf}"
                )

            noaa_id = str(current_id)
            try:
                if self._process_item(noaa_id, seen_urls):
                    saved += 1
                    consecutive_misses = 0
                else:
                    consecutive_misses += 1
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {noaa_id} failed: {exc}")
                consecutive_misses += 1

            current_id -= 1

        return saved
