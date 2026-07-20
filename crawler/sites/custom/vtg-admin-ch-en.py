# -*- coding: utf-8 -*-
"""Crawler for Swiss Peace Supporter magazine on vtg.admin.ch (English).

Source page lists all magazine issues as PDF download items with
short topic descriptions. Richer abstracts are extracted from the
po.zem.ch online reader's embedded SQLite database (meta.db).
"""

import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time

from crawler.base_crawler import BaseCrawler

_START_URL = "https://www.vtg.admin.ch/en/swiss-peace-supporter-4"
_PUBLISHER = (
    "Swiss Armed Forces; "
    "Federal Department of Defence, Civil Protection and Sport (DDPS); "
    "Federal Department of Foreign Affairs (FDFA)"
)
_MONTHS = {
    'January': 1, 'February': 2, 'March': 3, 'April': 4,
    'May': 5, 'June': 6, 'July': 7, 'August': 8,
    'September': 9, 'October': 10, 'November': 11, 'December': 12,
}
_SITE_ID = "vtg-admin-ch-en"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _curl_get(url, max_tries=3, as_bytes=False):
    """GET via curl with retries. Returns str (UTF-8) or bytes, None on failure."""
    cmd = ['curl', '--tls-max', '1.3', '-sk', '-L', '--max-time', '30', url]
    for attempt in range(max_tries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            if result.returncode == 0 and result.stdout:
                if as_bytes:
                    return result.stdout
                return result.stdout.decode('utf-8', errors='replace')
            if attempt < max_tries - 1:
                wait = 3 ** attempt
                print(f"[{_SITE_ID}] empty response (attempt {attempt + 1}), retry in {wait}s…")
                time.sleep(wait)
        except Exception as exc:
            if attempt < max_tries - 1:
                wait = 3 ** attempt
                print(f"[{_SITE_ID}] curl error: {exc}, retry in {wait}s…")
                time.sleep(wait)
            else:
                print(f"[{_SITE_ID}] curl failed after {max_tries} attempts: {exc}")
    return None


def _make_soup(html):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ('html5lib', 'lxml', 'html.parser'):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _parse_date(date_str):
    """'30 March 2026' → 'YYYY-MM-DD'. Returns None on parse failure."""
    if not date_str:
        return None
    m = re.match(r'(\d{1,2})\s+(\w+)\s+(\d{4})', date_str.strip())
    if m:
        day, mon_name, year = m.groups()
        mon = _MONTHS.get(mon_name)
        if mon:
            return f"{year}-{mon:02d}-{int(day):02d}"
    return None


def _fetch_zem_abstract(zem_url):
    """Download meta.db from po.zem.ch reader and return table of contents text.

    The po.zem.ch HTML5 magazine reader embeds content in an SQLite database
    (meta.db). Page 2 is always the table of contents with article titles in
    multiple languages — ideal as a structured abstract.
    """
    base = zem_url.rstrip('/')
    if base.endswith('index.html'):
        base = base[:-len('index.html')].rstrip('/')
    db_url = base + '/meta.db'

    raw = _curl_get(db_url, as_bytes=True)
    if not raw or len(raw) < 1000:
        return ''

    tmppath = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tf:
            tf.write(raw)
            tmppath = tf.name

        conn = sqlite3.connect(tmppath)
        try:
            rows = conn.execute(
                "SELECT content FROM PAGES WHERE pagenumber = '2' ORDER BY id LIMIT 1"
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return ''

        content = rows[0][0] or ''
        # Keep only lines with at least 3 alphabetic chars; join meaningfully
        lines = [l.strip() for l in content.split('\n') if l.strip()]
        lines = [l for l in lines if re.search(r'[a-zA-Z]{3,}', l)]
        return ' | '.join(lines[:20]).strip()

    except Exception as exc:
        print(f"[{_SITE_ID}] meta.db parse error for {zem_url}: {exc}")
        return ''
    finally:
        if tmppath:
            try:
                os.unlink(tmppath)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class VtgAdminChEnCrawler(BaseCrawler):
    site_id = "vtg-admin-ch-en"
    site_name = "Custom: vtg-admin-ch-en"
    base_url = "https://www.vtg.admin.ch"

    def crawl(self, limit=None):
        """Crawl Swiss Peace Supporter magazine issues.

        The listing page is fully server-side rendered — all 17 issues appear
        in the initial HTML response, so no pagination is needed. For each
        issue, a richer abstract is fetched from the po.zem.ch reader's
        embedded SQLite database; a combined issue + page description is used
        as fallback.
        """
        saved = 0
        limit_disp = str(limit) if limit is not None else "∞"
        seen_urls = set()
        start_time = time.time()
        MAX_SECONDS = 25 * 60  # 25-minute wall-clock budget

        # ── Fetch listing page ─────────────────────────────────────────
        print(f"[{self.site_id}] fetching {_START_URL}")
        html = _curl_get(_START_URL)
        if not html:
            print(f"[{self.site_id}] failed to fetch main page")
            return 0

        soup = _make_soup(html)
        if not soup:
            print(f"[{self.site_id}] HTML parse failed")
            return 0

        # ── Extract download items ────────────────────────────────────
        items = soup.select('a.download-item')
        print(f"[{self.site_id}] found {len(items)} download items on listing page")
        if not items:
            print(f"[{self.site_id}] no items found — selector may have changed")
            return 0

        # ── Map po.zem.ch reader links by display text ────────────────
        # e.g. "Swiss Peace Supporter 2026/1" → URL
        zem_links = {}
        for a in soup.find_all('a', href=True):
            href = a.get('href', '')
            if 'zem.ch' in href:
                text = a.get_text(strip=True)
                zem_links[text] = href

        # ── Page-level meta description (fallback abstract component) ─
        meta_el = soup.find('meta', {'name': 'description'})
        page_desc = meta_el.get('content', '').strip() if meta_el else (
            "Swiss Peace Supporter is the multilingual magazine covering "
            "Swiss contributions to international peace support operations, "
            "including peacekeeping missions, security policy analysis, and "
            "the work of Swiss military and civilian personnel abroad."
        )

        # ── Process each issue (single page — no pagination) ──────────
        # The site has one listing page; items are the complete dataset.
        for idx, item in enumerate(items):
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time > MAX_SECONDS:
                print(f"[{self.site_id}] 25-minute wall-clock budget exceeded, stopping")
                break

            try:
                title_el = item.select_one('.download-item__title')
                desc_el = item.select_one('.download-item__description')
                meta_els = item.select('.meta-info__item')
                pdf_url = item.get('href', '').strip()
                dl_filename = item.get('download', '').strip() or None

                title = title_el.get_text(strip=True) if title_el else ''
                issue_desc = desc_el.get_text(strip=True) if desc_el else ''
                meta_info = [m.get_text(strip=True) for m in meta_els]
                # meta_info layout: ['PDF', '3.02 MB', '30 March 2026']
                date_str = meta_info[2] if len(meta_info) >= 3 else ''

                if not title or not pdf_url:
                    print(f"[{self.site_id}] item {idx}: missing title or PDF URL, skipping")
                    continue

                if pdf_url in seen_urls:
                    print(f"[{self.site_id}] item {idx}: duplicate URL, skipping")
                    continue
                seen_urls.add(pdf_url)

                # Parse year/issue: "SWISS PEACE SUPPORTER 2026/1"
                yr_m = re.search(r'(\d{4})/(\d+)', title)
                year = yr_m.group(1) if yr_m else ''
                issue_num = yr_m.group(2) if yr_m else ''
                # post_number: numeric string for incremental crawl tracking
                post_number = (
                    f"{year}{issue_num.zfill(2)}" if year and issue_num else None
                )

                # external_id: DAM hash from PDF URL (stable, unique per issue)
                dam_m = re.search(r'/sd-web/([^/]+)/', pdf_url)
                external_id = dam_m.group(1) if dam_m else post_number

                published_date = _parse_date(date_str)

                # Find matching po.zem.ch URL
                zem_key = (
                    f"Swiss Peace Supporter {year}/{issue_num}" if year else None
                )
                zem_url = zem_links.get(zem_key, '') if zem_key else ''

                # Build abstract: try po.zem.ch TOC first, then fallback
                abstract = ''
                if zem_url:
                    abstract = _fetch_zem_abstract(zem_url)
                    time.sleep(self._delay)

                if not abstract or len(abstract) < 100:
                    parts = [p for p in [issue_desc, page_desc] if p]
                    fallback = '. '.join(parts)
                    if not abstract or len(fallback) > len(abstract):
                        abstract = fallback

                if len(abstract) < 50:
                    print(
                        f"[{self.site_id}] item {idx} '{title}': "
                        f"abstract too short ({len(abstract)} chars), skipping"
                    )
                    continue

                paper = {
                    'site_id': self.site_id,
                    'external_id': external_id,
                    'post_number': post_number,
                    'title': title,
                    'abstract': abstract,
                    'published_date': published_date,
                    'listed_date': published_date,
                    'publisher': _PUBLISHER,
                    'url': _START_URL,
                    'pdf_url': pdf_url,
                    'original_filename': dl_filename,
                    'category': 'Magazine',
                    'keywords': (
                        'peacekeeping, Switzerland, military, peace support, '
                        'SWISSINT, SWISSCOY, international missions'
                    ),
                    'metadata': json.dumps({
                        'posted_date': date_str,
                        'originalFilename': dl_filename,
                        'issue': f"{year}/{issue_num}" if year else '',
                        'year': year,
                        'issue_number': issue_num,
                        'post_number': post_number,
                        'dam_hash': external_id,
                        'zem_url': zem_url,
                        'file_size': meta_info[1] if len(meta_info) >= 2 else '',
                    }, ensure_ascii=False),
                }

                self._save_paper(paper)
                saved += 1
                print(f"[{self.site_id}] saved {saved}/{limit_disp}: {title}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {idx} failed: {exc}")
                continue

        print(f"[{self.site_id}] done — total saved: {saved}")
        return saved
