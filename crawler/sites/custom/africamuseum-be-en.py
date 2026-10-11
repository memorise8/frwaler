# -*- coding: utf-8 -*-
"""Crawler for Royal Museum for Central Africa (AfricaMuseum) science news.

Target: https://www.africamuseum.be/en/research/news
Site: Drupal 10, paginated list (?page=N), HTML scraping.
"""

import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urljoin, urlsplit

sys.path.insert(0, '.')
from crawler.base_crawler import BaseCrawler

_MONTH_MAP = {
    'january': '01', 'february': '02', 'march': '03', 'april': '04',
    'may': '05', 'june': '06', 'july': '07', 'august': '08',
    'september': '09', 'october': '10', 'november': '11', 'december': '12',
}

_MONTH_PAT = re.compile(
    r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{4})',
    re.IGNORECASE,
)


def _parse_month_year(text):
    """'April 2026' → '2026-04', or None."""
    if not text:
        return None
    m = _MONTH_PAT.search(text)
    if m:
        return f"{m.group(2)}-{_MONTH_MAP[m.group(1).lower()]}"
    return None


def _strip_tags(html):
    """Remove HTML tags; collapse whitespace."""
    text = re.sub(r'<[^>]+>', ' ', html)
    return re.sub(r'\s+', ' ', text).strip()


def _make_soup(html):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ('html5lib', 'lxml', 'html.parser'):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


class AfricamuseumBeEnCrawler(BaseCrawler):
    """Science news crawler for www.africamuseum.be (English)."""

    site_id = "africamuseum-be-en"
    site_name = "Custom: africamuseum-be-en"
    base_url = "https://www.africamuseum.be"

    _LIST_BASE = "https://www.africamuseum.be/en/research/news"
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _RATE_SLEEP = 1.0
    _MIN_ABSTRACT = 100

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
                        '-H', f'User-Agent: {self.USER_AGENT}',
                        '-H', 'Accept: text/html,*/*;q=0.8',
                        '-H', 'Accept-Language: en-US,en;q=0.9',
                        url,
                    ],
                    capture_output=True,
                    timeout=35,
                )
                raw = result.stdout
                if raw:
                    try:
                        return raw.decode('utf-8')
                    except UnicodeDecodeError:
                        return raw.decode('utf-8', errors='replace')
                if attempt < retries - 1:
                    wait = 3 ** attempt  # 1s, 3s, 9s
                    print(f"[africamuseum-be-en] empty response (attempt {attempt + 1}), retry in {wait}s …")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < retries - 1:
                    wait = 3 ** attempt
                    print(f"[africamuseum-be-en] curl error (attempt {attempt + 1}): {exc}, retry in {wait}s …")
                    time.sleep(wait)
                else:
                    print(f"[africamuseum-be-en] curl failed after {retries} attempts for {url}: {exc}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_list_page(self, html):
        """Return (slug, title, listed_date, snippet, source_url) rows.

        Each Drupal views-row contains both an image link and a title link to
        the same article URL, so we deduplicate by slug within the page.
        """
        items = []
        url_positions = {}
        news_path = re.compile(r'^/en/(?:research/(?:discover/)?)?news/[^/?#]+$')

        def target(href):
            url = urljoin(self._LIST_BASE, href)
            parts = urlsplit(url)
            if parts.hostname == 'www.africamuseum.be' and news_path.match(parts.path):
                return parts.path.rsplit('/', 1)[-1], url
            return None
        try:
            soup = _make_soup(html)
            if soup is None:
                raise ValueError("BeautifulSoup returned None")

            rows = soup.find_all('div', class_=re.compile(r'views-row'))
            if not rows:
                links = dict.fromkeys(
                    t for a in soup.find_all('a', href=True)
                    if (t := target(a['href']))
                )
                return [(slug, None, None, None, url) for slug, url in links]

            for row in rows:
                try:
                    # Slug / URL
                    link = next((a for a in row.find_all('a', href=True)
                                 if target(a['href'])), None)
                    if not link:
                        continue
                    slug, source_url = target(link['href'])

                    # Title
                    h_tag = row.find(['h3', 'h2', 'h4', 'h1'])
                    title = h_tag.get_text(strip=True) if h_tag else link.get_text(strip=True)

                    # Date + snippet from visible text
                    full_text = row.get_text(' ', strip=True)
                    listed_date = _parse_month_year(full_text)

                    snippet_m = _MONTH_PAT.search(full_text)
                    snippet = full_text[snippet_m.end():].strip() if snippet_m else ''

                    # Drupal can put the image and text in separate views-row
                    # elements. Keep one identity while filling fields from the
                    # text row instead of discarding it after the image link.
                    if source_url in url_positions:
                        position = url_positions[source_url]
                        previous = items[position]
                        items[position] = (
                            slug, previous[1] or title,
                            previous[2] or listed_date, previous[3] or snippet, source_url,
                        )
                    else:
                        url_positions[source_url] = len(items)
                        items.append((slug, title, listed_date, snippet, source_url))
                except Exception as exc:
                    print(f"[africamuseum-be-en] row parse error: {exc}")
                    continue

        except Exception as exc:
            print(f"[africamuseum-be-en] list page parse error: {exc}")

        return items

    def _parse_detail_page(self, html, slug, listed_date=None, snippet=None, source_url=None):
        """Return paper dict or None. Skips items with short abstracts."""
        try:
            soup = _make_soup(html)
            if soup is None:
                raise ValueError("BeautifulSoup returned None")

            # Title: og:title > h1
            og_title = soup.find('meta', property='og:title')
            title = og_title.get('content', '').strip() if og_title else ''
            if not title:
                h1 = soup.find('h1')
                title = h1.get_text(strip=True) if h1 else ''
            if not title:
                print(f"[africamuseum-be-en] no title for {slug}, skipping")
                return None

            # Node ID via data-history-node-id attribute
            node_el = soup.find(attrs={'data-history-node-id': True})
            node_id = node_el.get('data-history-node-id') if node_el else None

            # Abstract: prefer field-description body (long), fallback to og:description
            abstract = ''
            desc_div = soup.find(class_=re.compile(r'field--name-field-description'))
            if desc_div:
                abstract = desc_div.get_text(' ', strip=True)

            if len(abstract) < self._MIN_ABSTRACT:
                og_desc = soup.find('meta', property='og:description')
                if og_desc:
                    abstract = og_desc.get('content', '').strip()

            if len(abstract) < self._MIN_ABSTRACT and snippet:
                abstract = snippet

            if len(abstract) < self._MIN_ABSTRACT:
                print(
                    f"[africamuseum-be-en] abstract too short ({len(abstract)} chars) "
                    f"for {slug}, skipping"
                )
                return None

            external_id = node_id or slug
            url = source_url or f"https://www.africamuseum.be/en/research/news/{slug}"

            return {
                'site_id': self.site_id,
                'external_id': external_id,
                'post_number': node_id or slug,
                'title': title,
                'abstract': abstract,
                'url': url,
                'published_date': listed_date,
                'listed_date': listed_date,
                'publisher': 'Royal Museum for Central Africa',
                'category': 'Science News',
                'metadata': json.dumps({
                    'node_id': node_id,
                    'slug': slug,
                    'posted_date': listed_date,
                }, ensure_ascii=False),
            }

        except Exception as exc:
            print(f"[africamuseum-be-en] detail parse error for {slug}: {exc}")
            return None

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        limit_str = str(limit) if limit is not None else 'inf'
        start_time = time.time()

        for page in range(self._MAX_PAGES):
            # Wall-clock budget
            elapsed = time.time() - start_time
            if elapsed > self._WALL_SECONDS:
                print(f"[africamuseum-be-en] wall-clock limit reached at page {page}, stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(f"[africamuseum-be-en] page {page}: saved {saved}/{limit_str}")

            url = f"{self._LIST_BASE}?page={page}" if page > 0 else self._LIST_BASE
            html = self._curl_get(url)
            if not html:
                print(f"[africamuseum-be-en] failed to fetch list page {page}, stopping.")
                break

            items = self._parse_list_page(html)
            if not items:
                print(f"[africamuseum-be-en] no items on page {page}, stopping.")
                break

            # Skip fully-seen pages (dedup guard against paginator loops)
            new_items = [
                (slug, t, d, sn, target_url) for slug, t, d, sn, target_url in items
                if target_url not in seen_urls
            ]
            if not new_items:
                print(f"[africamuseum-be-en] all items on page {page} already seen, stopping.")
                break

            for slug, title, listed_date, snippet, detail_url in new_items:
                if limit is not None and saved >= limit:
                    break

                seen_urls.add(detail_url)

                try:
                    time.sleep(self._RATE_SLEEP)
                    detail_html = self._curl_get(detail_url)
                    if not detail_html:
                        print(f"[africamuseum-be-en] failed to fetch detail: {slug}")
                        continue

                    paper = self._parse_detail_page(detail_html, slug, listed_date, snippet, detail_url)
                    if not paper:
                        continue

                    self._save_paper(paper)
                    if self._last_save_created:
                        saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[africamuseum-be-en] item {slug} failed: {exc}")
                    continue

        if page == self._MAX_PAGES - 1:
            print(f"[africamuseum-be-en] safety cap of {self._MAX_PAGES} pages reached.")

        print(f"[africamuseum-be-en] done: saved {saved} items.")
        return saved
