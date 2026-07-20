# -*- coding: utf-8 -*-
"""Crawler for Royal Museum for Central Africa (AfricaMuseum) science news.

Target: https://www.africamuseum.be/en/research/news
Site: Drupal 10, paginated list (?page=N), HTML scraping.
"""

import json
import re
import subprocess
import sys
import time

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
    _MAX_PAGES = 200
    _WALL_SECONDS = 25 * 60
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
        """Return list of (slug, title, listed_date, snippet) from one page.

        Each Drupal views-row contains both an image link and a title link to
        the same article URL, so we deduplicate by slug within the page.
        """
        items = []
        seen_slugs = set()
        try:
            soup = _make_soup(html)
            if soup is None:
                raise ValueError("BeautifulSoup returned None")

            rows = soup.find_all('div', class_=re.compile(r'views-row'))
            if not rows:
                # Fallback: regex slug extraction only (already deduped via dict.fromkeys)
                slugs = list(dict.fromkeys(
                    re.findall(r'/en/research/news/([a-zA-Z0-9_-]+)', html)
                ))
                return [(s, None, None, None) for s in slugs]

            for row in rows:
                try:
                    # Slug / URL
                    link = row.find('a', href=re.compile(r'/en/research/news/\w'))
                    if not link:
                        continue
                    href = link.get('href', '')
                    slug_m = re.search(r'/en/research/news/([a-zA-Z0-9_-]+)', href)
                    if not slug_m:
                        continue
                    slug = slug_m.group(1)

                    # Deduplicate within this page (image link + title link → same slug)
                    if slug in seen_slugs:
                        continue
                    seen_slugs.add(slug)

                    # Title
                    h_tag = row.find(['h3', 'h2', 'h4', 'h1'])
                    title = h_tag.get_text(strip=True) if h_tag else link.get_text(strip=True)

                    # Date + snippet from visible text
                    full_text = row.get_text(' ', strip=True)
                    listed_date = _parse_month_year(full_text)

                    snippet_m = _MONTH_PAT.search(full_text)
                    snippet = full_text[snippet_m.end():].strip() if snippet_m else ''

                    items.append((slug, title, listed_date, snippet))
                except Exception as exc:
                    print(f"[africamuseum-be-en] row parse error: {exc}")
                    continue

        except Exception as exc:
            print(f"[africamuseum-be-en] list page parse error: {exc}")

        return items

    def _parse_detail_page(self, html, slug, listed_date=None, snippet=None):
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
            url = f"https://www.africamuseum.be/en/research/news/{slug}"

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
                (slug, t, d, sn) for slug, t, d, sn in items
                if f"/en/research/news/{slug}" not in seen_urls
            ]
            if not new_items:
                print(f"[africamuseum-be-en] all items on page {page} already seen, stopping.")
                break

            for slug, title, listed_date, snippet in new_items:
                if limit is not None and saved >= limit:
                    break

                detail_url = f"https://www.africamuseum.be/en/research/news/{slug}"
                seen_urls.add(f"/en/research/news/{slug}")

                try:
                    time.sleep(self._RATE_SLEEP)
                    detail_html = self._curl_get(detail_url)
                    if not detail_html:
                        print(f"[africamuseum-be-en] failed to fetch detail: {slug}")
                        continue

                    paper = self._parse_detail_page(detail_html, slug, listed_date, snippet)
                    if not paper:
                        continue

                    self._save_paper(paper)
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
