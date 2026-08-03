# -*- coding: utf-8 -*-
"""Crawler for Swiss Federal Administration news (English press releases).

API: https://d-nsbc-p.admin.ch/v1/search
  - newsKinds[]=CONTENT_HUB&newsKinds[]=ONSB
  - newsCategoryIDs[]=medienmitteilung
  - offset / limit / sort=DESC
  - Items have lang field; filter lang=='en' for English-only items.
  - Detail URL: https://www.news.admin.ch/en/newnsb/{langGroupId}
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from html import unescape
from urllib.parse import quote

from crawler.base_crawler import BaseCrawler

_HTML_TAG_RE = re.compile(r'<[^>]+>')


def _strip_html(html_text: str) -> str:
    if not html_text:
        return ''
    text = _HTML_TAG_RE.sub(' ', html_text)
    text = unescape(text)
    return re.sub(r'\s+', ' ', text).strip()


class NewsAdminChEnCrawler(BaseCrawler):
    site_id = 'news-admin-ch-en'
    site_name = 'Custom: news-admin-ch-en'
    base_url = 'https://www.news.admin.ch'

    _API_BASE = 'https://d-nsbc-p.admin.ch/v1'
    _DETAIL_BASE = 'https://www.news.admin.ch/en/newnsb'
    _START_DATE = '2024-08-29T00:00:00.000Z'
    _END_DATE = '2025-08-29T23:59:59.999Z'
    _PAGE_SIZE = 50
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _MIN_ABSTRACT_CHARS = 50
    _BACKOFF = (1, 3, 9)

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _fetch_json(self, url: str, params: list[tuple]) -> dict | None:
        """Fetch JSON from url with query params via curl; retry with backoff."""
        qs_parts = [f'{quote(str(k))}={quote(str(v))}' for k, v in params]
        full_url = f'{url}?{"&".join(qs_parts)}'

        for attempt, wait in enumerate([0] + list(self._BACKOFF)):
            if wait:
                time.sleep(wait)
            try:
                proc = subprocess.run(
                    ['curl', '--tls-max', '1.3', '-sk',
                     '-H', 'Accept: application/json',
                     '-H', f'Origin: {self.base_url}',
                     '-H', f'Referer: {self.base_url}/',
                     '--max-time', '45',
                     full_url],
                    capture_output=True, timeout=60,
                )
                body = proc.stdout.decode('utf-8', errors='replace').strip()
                if body:
                    return json.loads(body)
                print(f'[{self.site_id}] empty response (attempt {attempt + 1})')
            except subprocess.TimeoutExpired:
                print(f'[{self.site_id}] curl timeout (attempt {attempt + 1})')
            except json.JSONDecodeError as exc:
                print(f'[{self.site_id}] JSON parse error (attempt {attempt + 1}): {exc}')
            except Exception as exc:
                print(f'[{self.site_id}] curl error (attempt {attempt + 1}): {exc}')

        return None

    # ------------------------------------------------------------------
    # Abstract builder
    # ------------------------------------------------------------------

    def _build_abstract(self, item: dict) -> str:
        desc = _strip_html(item.get('description', ''))
        if len(desc) >= 100:
            return desc

        parts = [desc] if desc else []
        for para in item.get('text', []):
            stripped = _strip_html(para)
            if not stripped:
                continue
            parts.append(stripped)
            if len(' '.join(parts)) >= 100:
                break

        return ' '.join(parts).strip()

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl Swiss Federal Administration English press releases."""
        if limit is not None and limit <= 0:
            return 0

        start_time = time.monotonic()
        limit_or_inf = limit if limit is not None else 'inf'
        saved = 0
        seen_urls: set[str] = set()
        offset = 0
        page = 0

        base_params: list[tuple] = [
            ('newsKinds[]', 'CONTENT_HUB'),
            ('newsKinds[]', 'ONSB'),
            ('newsCategoryIDs[]', 'medienmitteilung'),
            ('start_date', self._START_DATE),
            ('end_date', self._END_DATE),
            ('sort', 'DESC'),
            ('limit', self._PAGE_SIZE),
        ]

        while True:
            # Wall-clock budget
            elapsed = time.monotonic() - start_time
            if elapsed >= self._MAX_WALL_SECONDS:
                print(f'[{self.site_id}] Wall-clock budget exceeded ({elapsed:.0f}s), stopping.')
                break

            # Safety page cap
            if page >= self._MAX_PAGES:
                print(f'[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached, stopping.')
                break

            page += 1
            params = base_params + [('offset', offset)]
            data = self._fetch_json(self._API_BASE + '/search', params)

            if data is None:
                print(f'[{self.site_id}] Failed to fetch page {page} at offset {offset}, stopping.')
                break

            items = data.get('items') or []
            if not items:
                print(f'[{self.site_id}] No items at offset {offset}, done.')
                break

            en_items = [i for i in items
                        if i.get('lang') == 'en' and i.get('langGroupId')]

            if page % 10 == 0 or page <= 3:
                print(f'[{self.site_id}] page {page} (offset {offset}): '
                      f'{len(en_items)} EN / {len(items)} total | saved {saved}/{limit_or_inf}')

            for item in en_items:
                if limit is not None and saved >= limit:
                    break

                try:
                    lang_group_id = item.get('langGroupId', '')
                    detail_url = f'{self._DETAIL_BASE}/{lang_group_id}'

                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)

                    abstract = self._build_abstract(item)
                    if len(abstract) < self._MIN_ABSTRACT_CHARS:
                        print(f'[{self.site_id}] item {item.get("id")} '
                              f'abstract too short ({len(abstract)} chars), skipping.')
                        continue

                    content = item.get('content') or {}
                    meta = content.get('metadata') or {}
                    sys_data = content.get('systemdata') or {}

                    external_id = item.get('id', '')
                    doc_id = sys_data.get('documentId')
                    post_number = str(doc_id) if doc_id is not None else None

                    title = (item.get('title')
                             or meta.get('metaTitle')
                             or meta.get('title') or '')
                    if not title:
                        print(f'[{self.site_id}] item {external_id} no title, skipping.')
                        continue

                    pub_raw = sys_data.get('firstPublicationDate') or item.get('publishDate', '')
                    published_date = pub_raw[:10] if pub_raw else None

                    announce_raw = meta.get('announcementDate', '')
                    listed_date = announce_raw[:10] if announce_raw else published_date

                    publishers_list = item.get('publishers') or []
                    publisher = ';'.join(publishers_list) if publishers_list else None

                    topics = item.get('topics') or []
                    keywords = ','.join(topics) if topics else None

                    metadata_obj = {
                        'posted_date': announce_raw or pub_raw,
                        'lang': item.get('lang'),
                        'kind': item.get('kind'),
                        'newsCategory': item.get('newsCategory'),
                        'langGroupId': lang_group_id,
                        'publishers': publishers_list,
                        'topics': topics,
                        'location': item.get('location', ''),
                        'documentId': doc_id,
                        'slug': meta.get('slug'),
                    }

                    paper = {
                        'site_id': self.site_id,
                        'external_id': external_id,
                        'post_number': post_number,
                        'title': title,
                        'abstract': abstract,
                        'url': detail_url,
                        'pdf_url': None,
                        'published_date': published_date,
                        'posted_date': listed_date,
                        'authors': None,
                        'publisher': publisher,
                        'department': None,
                        'journal': None,
                        'keywords': keywords,
                        'category': item.get('newsCategory'),
                        'doi': None,
                        'original_filename': None,
                        'metadata': json.dumps(metadata_obj, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f'[{self.site_id}] item {item.get("id", "?")} failed: {exc}')
                    continue

            if limit is not None and saved >= limit:
                break

            # Check if we've exhausted all results
            page_results = data.get('pageResults', 0)
            if offset + self._PAGE_SIZE >= page_results:
                print(f'[{self.site_id}] Reached end of results (pageResults={page_results}).')
                break

            offset += self._PAGE_SIZE
            time.sleep(self._delay)

        print(f'[{self.site_id}] Done. Saved {saved} items.')
        return saved
