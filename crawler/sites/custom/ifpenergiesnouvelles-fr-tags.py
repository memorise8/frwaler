# -*- coding: utf-8 -*-
"""IFP Energies nouvelles — communiqués de presse tag crawler.

Target: https://www.ifpenergiesnouvelles.fr/tags/communiques-presse
API:    Drupal Views AJAX  POST /views/ajax  page=0, 1, 2, …
"""

import json
import re
import time
from urllib.parse import urljoin

from crawler.base_crawler import BaseCrawler

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_AJAX_URL   = "https://www.ifpenergiesnouvelles.fr/views/ajax"
_TAG_TID    = "72"          # taxonomy term ID for communiques-presse
_VIEW_DOM_ID = "ifpen_cp_dom"
_SAFETY_PAGE_CAP  = 200
_WALL_CLOCK_LIMIT = 25 * 60  # 25 minutes

# ---------------------------------------------------------------------------
# French month → zero-padded number
# ---------------------------------------------------------------------------
_FRENCH_MONTHS = {
    'janvier': '01', 'février': '02', 'fevrier': '02',
    'mars': '03',    'avril': '04',   'mai': '05',
    'juin': '06',    'juillet': '07', 'août': '08', 'aout': '08',
    'septembre': '09', 'octobre': '10', 'novembre': '11',
    'décembre': '12', 'decembre': '12',
}


def _parse_french_date(text):
    """'DD mois YYYY' → 'YYYY-MM-DD', or None."""
    if not text:
        return None
    m = re.search(r'(\d{1,2})\s+(\w+)\s+(\d{4})', text.strip())
    if not m:
        return None
    day, month_raw, year = m.group(1), m.group(2).lower(), m.group(3)
    month_num = _FRENCH_MONTHS.get(month_raw)
    if not month_num:
        return None
    return f"{year}-{month_num}-{day.zfill(2)}"


def _parse_dot_date(text):
    """'DD.MM.YYYY' → 'YYYY-MM-DD', or None."""
    if not text:
        return None
    m = re.match(r'(\d{2})\.(\d{2})\.(\d{4})', text.strip())
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def _make_soup(html):
    """BeautifulSoup with fallback: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ('html5lib', 'lxml', 'html.parser'):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _strip_html(raw):
    """Strip HTML tags and normalise whitespace."""
    text = re.sub(r'<[^>]+>', ' ', raw or '')
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&[a-zA-Z0-9#]+;', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class IFPEnergiesNouvellesFrTagsCrawler(BaseCrawler):
    """Crawler for IFPEN communiqués de presse tag page."""

    site_id   = "ifpenergiesnouvelles-fr-tags"
    site_name = "Custom: ifpenergiesnouvelles-fr-tags"
    base_url  = "https://www.ifpenergiesnouvelles.fr"

    # ------------------------------------------------------------------ #
    # List page fetching
    # ------------------------------------------------------------------ #

    def _fetch_list_page(self, page_num):
        """POST to Drupal Views AJAX; return combined insert-command HTML or None."""
        post_data = {
            "view_name":       "news_on_tags",
            "view_display_id": "block_1",
            "view_args":       _TAG_TID,
            "view_path":       f"/taxonomy/term/{_TAG_TID}",
            "view_base_path":  "",
            "view_dom_id":     _VIEW_DOM_ID,
            "pager_element":   "0",
            "page":            str(page_num),
        }
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": f"{self.base_url}/tags/communiques-presse",
        }
        for attempt in range(3):
            try:
                resp = self._session.post(
                    _AJAX_URL, data=post_data, headers=headers, timeout=30
                )
                resp.raise_for_status()
                data = resp.json()
                parts = [
                    cmd.get('data', '')
                    for cmd in data
                    if isinstance(cmd, dict) and cmd.get('command') == 'insert'
                       and cmd.get('data')
                ]
                return '\n'.join(parts) if parts else None
            except Exception as exc:
                wait = (attempt + 1) ** 2
                print(f"[{self.site_id}] list page {page_num} attempt {attempt+1}/3: {exc}")
                if attempt < 2:
                    time.sleep(wait)
        return None

    def _parse_list_html(self, html):
        """Return list of (url, listed_date_str, title, category) from list HTML."""
        results = []
        # Split on views-row divs; each row holds one article card
        rows = re.split(r'<div class="views-row[^"]*"', html)
        for row in rows[1:]:
            url_m = re.search(
                r'href="(https://www\.ifpenergiesnouvelles\.fr/article/[^"]+)"',
                row
            )
            if not url_m:
                continue
            url = url_m.group(1)
            title_m = re.search(r'<h3[^>]*actu_titre[^>]*>\s*<a[^>]*>([^<]+)</a>', row)
            title = title_m.group(1).strip() if title_m else ''
            date_m = re.search(r'class="datePost[^"]*">([^<]+)<', row)
            listed_date_str = date_m.group(1).strip() if date_m else ''
            cat_m = re.search(r'class="categ[^"]*">([^<]+)<', row)
            category = cat_m.group(1).strip() if cat_m else ''
            results.append((url, listed_date_str, title, category))
        return results

    # ------------------------------------------------------------------ #
    # Detail page fetching + parsing
    # ------------------------------------------------------------------ #

    def _fetch_detail(self, url):
        """GET article detail page; return decoded HTML string or None."""
        for attempt in range(3):
            try:
                resp = self._session.get(url, timeout=30)
                resp.raise_for_status()
                try:
                    return resp.content.decode('utf-8')
                except UnicodeDecodeError:
                    return resp.content.decode('utf-8', errors='replace')
            except Exception as exc:
                wait = (attempt + 1) ** 2
                print(
                    f"[{self.site_id}] detail attempt {attempt+1}/3 "
                    f"for {url}: {exc}"
                )
                if attempt < 2:
                    time.sleep(wait)
        return None

    def _parse_detail(self, html, url):
        """Parse article detail HTML; return field dict."""
        result = {
            'node_id': None, 'title': '', 'published_date': None,
            'abstract': '', 'keywords': '', 'category': '', 'pdf_url': None,
        }

        # Node ID from language-switcher nav links
        node_m = re.search(r'data-drupal-link-system-path="node/(\d+)"', html)
        if node_m:
            result['node_id'] = node_m.group(1)

        # Scope to <main>…</main> to avoid header/footer noise
        main_start = html.find('<main ')
        main_end   = html.find('</main>', main_start)
        main_html  = (
            html[main_start:main_end]
            if main_start >= 0 and main_end > main_start
            else html
        )

        # ---- BeautifulSoup parse ----------------------------------------
        try:
            soup = _make_soup(main_html)
        except Exception:
            soup = None

        if soup:
            # Title
            h1 = soup.find('h1')
            if h1:
                result['title'] = h1.get_text(separator=' ', strip=True)
            if not result['title']:
                span = soup.find(attrs={'property': 'schema:name'})
                result['title'] = (span.get('content', '') or '').strip() if span else ''

            # Date: <p class="date">DD.MM.YYYY</p>
            date_p = soup.find('p', class_='date')
            if date_p:
                result['published_date'] = _parse_dot_date(date_p.get_text(strip=True))

            # Body / abstract
            body_div = soup.find(
                'div',
                class_=lambda c: c and 'field--name-body' in c
            )
            if body_div:
                raw_text = body_div.get_text(separator=' ')
                result['abstract'] = re.sub(r'\s+', ' ', raw_text).strip()
            else:
                # fallback: meta description
                meta = soup.find('meta', attrs={'name': 'description'})
                result['abstract'] = (meta.get('content', '') or '').strip() if meta else ''

            # Keywords: tags in ul.field--items
            tags_ul = soup.find('ul', class_='field--items')
            if tags_ul:
                tags = [a.get_text(strip=True) for a in tags_ul.find_all('a')]
                result['keywords'] = ','.join(
                    t for t in tags if t and 'communiqu' not in t.lower()
                )

            # Category
            cat_div = soup.find(
                'div',
                class_=lambda c: c and 'field--name-field-categorie-article' in c
            )
            if cat_div:
                a = cat_div.find('a')
                result['category'] = a.get_text(strip=True) if a else ''

            # PDF link
            pdf_a = soup.find('a', href=re.compile(r'\.pdf', re.I))
            if pdf_a:
                result['pdf_url'] = urljoin(self.base_url, pdf_a['href'])

        else:
            # ---- Pure-regex fallback ------------------------------------
            title_m = re.search(r'<h1[^>]*>(.*?)</h1>', main_html, re.DOTALL)
            result['title'] = _strip_html(title_m.group(1)) if title_m else ''

            date_m = re.search(r'<p class="date">(\d{2}\.\d{2}\.\d{4})</p>', main_html)
            result['published_date'] = _parse_dot_date(date_m.group(1)) if date_m else None

            body_idx = main_html.find('field--name-body')
            if body_idx >= 0:
                snip = main_html[body_idx:body_idx + 20000]
                gt = snip.find('>')
                result['abstract'] = _strip_html(snip[gt + 1:gt + 10001])
            else:
                meta_m = re.search(
                    r'<meta[^>]+name="description"[^>]+content="([^"]+)"', html
                )
                result['abstract'] = meta_m.group(1).strip() if meta_m else ''

        return result

    # ------------------------------------------------------------------ #
    # Main crawl
    # ------------------------------------------------------------------ #

    def crawl(self, limit=None):
        """Paginate IFPEN communiqués de presse tag via Drupal Views AJAX.

        Fetches each article detail page for full-body abstract.
        Stops when limit is reached, page returns no new URLs, or safety
        cap is hit.
        """
        saved       = 0
        seen_urls   = set()
        start_time  = time.time()
        limit_val   = limit if limit is not None else float('inf')

        for page_num in range(_SAFETY_PAGE_CAP):
            # Wall-clock safety budget
            elapsed = time.time() - start_time
            if elapsed > _WALL_CLOCK_LIMIT:
                print(
                    f"[{self.site_id}] Wall-clock limit "
                    f"({_WALL_CLOCK_LIMIT}s) reached at page {page_num}. Exiting."
                )
                break

            if saved >= limit_val:
                break

            if page_num % 10 == 0 and page_num > 0:
                print(
                    f"[{self.site_id}] page {page_num}: "
                    f"saved {saved}/{limit if limit else '∞'}"
                )

            if page_num == _SAFETY_PAGE_CAP - 1:
                print(f"[{self.site_id}] Safety cap of {_SAFETY_PAGE_CAP} pages reached.")

            list_html = self._fetch_list_page(page_num)
            if not list_html:
                print(f"[{self.site_id}] page {page_num}: empty response. Stopping.")
                break

            items = self._parse_list_html(list_html)
            if not items:
                print(f"[{self.site_id}] page {page_num}: no items parsed. Stopping.")
                break

            new_items = [
                (u, d, t, c) for u, d, t, c in items if u not in seen_urls
            ]
            if not new_items:
                print(
                    f"[{self.site_id}] page {page_num}: "
                    "all items already seen (loop guard). Stopping."
                )
                break

            for url, listed_date_str, list_title, list_category in new_items:
                if saved >= limit_val:
                    break
                seen_urls.add(url)

                try:
                    time.sleep(self._delay)

                    detail_html = self._fetch_detail(url)
                    if not detail_html:
                        print(f"[{self.site_id}] item skipped (fetch failed): {url}")
                        continue

                    detail = self._parse_detail(detail_html, url)

                    title = detail.get('title') or list_title
                    if not title:
                        print(f"[{self.site_id}] item skipped (no title): {url}")
                        continue

                    abstract = detail.get('abstract', '')
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item skipped "
                            f"(abstract {len(abstract)} chars < 50): {url}"
                        )
                        continue

                    node_id        = detail.get('node_id')
                    published_date = detail.get('published_date')
                    listed_date    = _parse_french_date(listed_date_str)
                    url_slug       = url.rstrip('/').split('/')[-1]
                    external_id    = node_id or url_slug

                    paper = {
                        "site_id":           self.site_id,
                        "external_id":       external_id,
                        "post_number":       node_id,
                        "title":             title,
                        "abstract":          abstract,
                        "published_date":    published_date or listed_date,
                        "listed_date":       listed_date,
                        "url":               url,
                        "pdf_url":           detail.get('pdf_url'),
                        "authors":           "",
                        "publisher":         "IFP Energies nouvelles",
                        "department":        "",
                        "journal":           "",
                        "doi":               "",
                        "keywords":          detail.get('keywords', ''),
                        "category":          detail.get('category') or list_category,
                        "original_filename": None,
                        "metadata": json.dumps({
                            "node_id":          node_id,
                            "posted_date":      listed_date_str,
                            "listed_date_raw":  listed_date_str,
                            "url_slug":         url_slug,
                            "tags": (
                                detail.get('keywords', '').split(',')
                                if detail.get('keywords') else []
                            ),
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    lbl = f"{limit}" if limit else "∞"
                    print(
                        f"[{self.site_id}] Saved {saved}/{lbl}: "
                        f"{title[:60]}"
                    )

                except KeyboardInterrupt:
                    print(
                        f"[{self.site_id}] Interrupted by user. "
                        f"Saved so far: {saved}"
                    )
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item failed ({url}): {exc}")
                    continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
