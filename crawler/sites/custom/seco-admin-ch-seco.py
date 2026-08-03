# -*- coding: utf-8 -*-
"""Crawler for State Secretariat for Economic Affairs (SECO) news on
seco.admin.ch.

Starting URL: https://www.seco.admin.ch/seco/en/home/seco/nsb-news.html
              ?dyn_startDate=01.01.2023

The site is a Nuxt SPA; the old JCR fragment endpoint
(``.../nsbnewslist.entries.html``) used by a prior version of this
crawler now returns 404. The "nsb-news" widget is powered by the shared
Swiss federal administration news broker API (the same backend used by
news.admin.ch): https://d-nsbc-p.admin.ch/v1/search

Key finding: ``publisherIDs=<id>`` (a plain scalar, NOT an array-style
``publisherIDs[]=<id>``) filters results down to a single federal office.
SECO's publisher id is ``60`` (discovered via the "Alle Medienmitteilungen
des SECO" link embedded in the page's hydration payload:
``/de/newnsb?publisherIDs=60&topicIDs=all&newsCategoryIDs=all``).
Using the bracket form silently disables the filter and returns items
from every federal office.

Each search result already carries the full article body, so no separate
detail-page fetch is needed. Two item "kinds" are returned:

- ``CONTENT_HUB`` — newer articles; content lives under
  ``content.metadata`` / ``content.systemdata``.
- ``ONSB`` — older-style press releases; content lives under
  ``content.content`` (departments, originators, attachments/PDFs).
- ``LINK`` — empty pointer stubs with no title/body; always skipped.

Detail URL pattern for both kinds: ``{base_url}/{lang}/newnsb/{langGroupId}``.
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


def _strip_html(text: str) -> str:
    if not text:
        return ''
    stripped = _HTML_TAG_RE.sub(' ', text)
    stripped = unescape(stripped)
    return re.sub(r'\s+', ' ', stripped).strip()


class SecoAdminChSecoCrawler(BaseCrawler):
    site_id = 'seco-admin-ch-seco'
    site_name = 'Custom: seco-admin-ch-seco'
    base_url = 'https://www.seco.admin.ch'

    _API_URL = 'https://d-nsbc-p.admin.ch/v1/search'
    _PUBLISHER_ID = '60'
    _PUBLISHER_NAME = 'State Secretariat for Economic Affairs (SECO)'
    _START_DATE = '2023-01-01T00:00:00.000Z'
    _END_DATE = '2030-12-31T23:59:59.999Z'
    _PAGE_SIZE = 100
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _MIN_ABSTRACT_CHARS = 100
    _BACKOFF = (1, 3, 9)

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _fetch_json(self, params: list) -> dict | None:
        """Fetch JSON from the search API via curl; retry with backoff."""
        qs = '&'.join(f'{quote(str(k))}={quote(str(v))}' for k, v in params)
        url = f'{self._API_URL}?{qs}'

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
                     url],
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
    # Parsing helpers
    # ------------------------------------------------------------------

    def _build_abstract(self, item: dict) -> str:
        desc = _strip_html(item.get('description') or '')
        if len(desc) >= self._MIN_ABSTRACT_CHARS:
            return desc

        parts = [desc] if desc else []
        for para in (item.get('text') or []):
            stripped = _strip_html(para)
            if not stripped:
                continue
            parts.append(stripped)
            if len(' '.join(parts)) >= self._MIN_ABSTRACT_CHARS:
                break

        return ' '.join(p for p in parts if p).strip()

    def _parse_item(self, item: dict) -> dict | None:
        """Extract a paper dict from one search-result item, or None to skip."""
        kind = item.get('kind')
        if kind not in ('CONTENT_HUB', 'ONSB'):
            return None  # LINK stubs and unknown kinds carry no content

        lang = item.get('lang') or ''
        if lang != 'en':
            return None  # site's English page -> English content only

        lang_group_id = item.get('langGroupId') or ''
        if not lang_group_id:
            return None

        title = (item.get('title') or '').strip()
        if not title:
            return None

        content = item.get('content') or {}
        publish_raw = item.get('publishDate') or ''
        published_date = publish_raw[:10] if publish_raw else None
        listed_date = published_date
        posted_date_raw = publish_raw
        document_id = None
        department = None
        publisher = self._PUBLISHER_NAME
        pdf_url = None
        original_filename = None
        keywords = None
        native_meta = {'kind': kind, 'lang': lang, 'langGroupId': lang_group_id}

        if kind == 'CONTENT_HUB':
            meta = content.get('metadata') or {}
            sysdata = content.get('systemdata') or {}
            document_id = sysdata.get('documentId')

            first_pub = sysdata.get('firstPublicationDate') or publish_raw
            if first_pub:
                published_date = first_pub[:10]

            announce_raw = meta.get('announcementDate') or ''
            if announce_raw:
                listed_date = announce_raw[:10]
                posted_date_raw = announce_raw

            native_meta.update({
                'documentId': document_id,
                'gdNewsCategory': meta.get('gdNewsCategory'),
                'slug': meta.get('slug'),
            })

        elif kind == 'ONSB':
            cc = content.get('content') or {}
            document_id = cc.get('id') or lang_group_id

            pub_date = cc.get('publicationDate') or ''
            if pub_date:
                published_date = pub_date

            depts = cc.get('departments') or []
            if depts and depts[0].get('name'):
                department = depts[0]['name']

            originators = cc.get('originators') or []
            orig_names = [o.get('name') for o in originators if o.get('name')]
            if orig_names:
                publisher = '; '.join(orig_names)

            attachments = cc.get('attachments') or []
            pdf_attachments = [
                a for a in attachments
                if (a.get('format') or '').lower() == 'pdf' and a.get('uri')
            ]
            if pdf_attachments:
                pdf_url = pdf_attachments[0]['uri']
                att_title = pdf_attachments[0].get('title')
                original_filename = f'{att_title}.pdf' if att_title else pdf_url.rsplit('/', 1)[-1]

            kw_raw = cc.get('keywords') or []
            kw_names = [k.get('name') if isinstance(k, dict) else str(k) for k in kw_raw]
            kw_names = [k for k in kw_names if k]
            if kw_names:
                keywords = ', '.join(kw_names)

            native_meta.update({
                'documentId': document_id,
                'onsbType': cc.get('type'),
            })

        abstract = self._build_abstract(item)
        if len(abstract) < self._MIN_ABSTRACT_CHARS:
            print(f'[{self.site_id}] item {item.get("id")} abstract too short '
                  f'({len(abstract)} chars), skipping.')
            return None

        url = f'{self.base_url}/{lang}/newnsb/{lang_group_id}'
        post_number = str(document_id) if document_id is not None else None

        topics = item.get('topics') or []
        if topics:
            native_meta['topics'] = topics

        metadata_obj = {
            'posted_date': posted_date_raw,
            'newsCategory': item.get('newsCategory'),
            'publishers': item.get('publishers'),
            'coPublishers': item.get('coPublishers'),
        }
        if original_filename:
            metadata_obj['originalFilename'] = original_filename
        metadata_obj.update(native_meta)

        return {
            'site_id': self.site_id,
            'external_id': item.get('id'),
            'post_number': post_number,
            'title': title,
            'abstract': abstract,
            'published_date': published_date,
            'posted_date': listed_date,
            'authors': None,
            'publisher': publisher,
            'department': department,
            'journal': None,
            'url': url,
            'pdf_url': pdf_url,
            'keywords': keywords,
            'category': item.get('newsCategory') or None,
            'doi': None,
            'original_filename': original_filename,
            'metadata': json.dumps(metadata_obj, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl SECO news via the shared federal news-broker search API."""
        if limit is not None and limit <= 0:
            return 0

        start_time = time.monotonic()
        limit_or_inf = limit if limit is not None else 'inf'
        saved = 0
        seen_urls: set = set()
        offset = 0
        page = 0

        base_params = [
            ('publisherIDs', self._PUBLISHER_ID),
            ('start_date', self._START_DATE),
            ('end_date', self._END_DATE),
            ('sort', 'DESC'),
            ('limit', self._PAGE_SIZE),
        ]

        try:
            while True:
                elapsed = time.monotonic() - start_time
                if elapsed >= self._MAX_WALL_SECONDS:
                    print(f'[{self.site_id}] Wall-clock budget exceeded ({elapsed:.0f}s), stopping.')
                    break

                if page >= self._MAX_PAGES:
                    print(f'[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached, stopping.')
                    break

                page += 1
                params = base_params + [('offset', offset)]
                data = self._fetch_json(params)

                if data is None:
                    print(f'[{self.site_id}] Failed to fetch page {page} at offset {offset}, stopping.')
                    break

                items = data.get('items') or []
                if not items:
                    print(f'[{self.site_id}] No items at offset {offset}, done.')
                    break

                new_this_page = 0
                for item in items:
                    if limit is not None and saved >= limit:
                        break

                    item_id = item.get('id', '?')
                    try:
                        paper = self._parse_item(item)
                        if paper is None:
                            continue

                        if paper['url'] in seen_urls:
                            continue
                        seen_urls.add(paper['url'])

                        self._save_paper(paper)
                        saved += 1
                        new_this_page += 1
                        print(f'[{self.site_id}] Saved {saved}/{limit_or_inf}: {paper["title"][:60]}')

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f'[{self.site_id}] item {item_id} failed: {exc}; continuing.')
                        continue

                if page % 10 == 0:
                    print(f'[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}')

                if limit is not None and saved >= limit:
                    break

                page_results = data.get('pageResults', 0)
                if offset + self._PAGE_SIZE >= page_results:
                    print(f'[{self.site_id}] Reached end of results (pageResults={page_results}).')
                    break

                offset += self._PAGE_SIZE
                time.sleep(self._delay)

        except KeyboardInterrupt:
            print(f'[{self.site_id}] Interrupted by user. Saved {saved} so far.')
            raise

        print(f'[{self.site_id}] Done. Saved {saved} items.')
        return saved
