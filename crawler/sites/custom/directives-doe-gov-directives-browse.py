# -*- coding: utf-8 -*-
"""DOE Directives Library crawler — https://www.directives.doe.gov/directives-browse
(redirects to https://www.energy.gov/management/directives-library)

Strategy:
  1. Fetch the directives library page which embeds all 205 records in an
     application/ld+json Dataset block (ID, Effective Date, Title+URL).
  2. For each record, download the PDF and extract the first few pages of
     text as the abstract (no separate HTML detail page exists).
"""

import io
import json
import re
import subprocess
import sys
import os
import time
from datetime import datetime
from html import unescape

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from crawler.base_crawler import BaseCrawler

_BS4_PARSERS = ['html5lib', 'lxml', 'html.parser']

SITE_ID = "directives-doe-gov-directives-browse"
LIBRARY_URL = "https://www.energy.gov/management/directives-library"

_TYPE_MAP = {
    'O': 'Order',
    'G': 'Guide',
    'N': 'Notice',
    'P': 'Policy',
    'M': 'Manual',
    'A': 'Announcement',
}


def _make_soup(html):
    for parser in _BS4_PARSERS:
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _curl_get(url, retries=3, binary=False):
    cmd = [
        'curl', '-skL', '--tls-max', '1.3', '--max-time', '60',
        '-H', 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        '-H', 'Accept: */*',
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=65)
            raw = result.stdout
            if raw and len(raw) > 100:
                if binary:
                    return raw
                try:
                    return raw.decode('utf-8', errors='replace')
                except Exception:
                    return raw.decode('latin-1', errors='replace')
            if attempt < retries - 1:
                wait = [1, 3, 9][attempt]
                print(f"[{SITE_ID}] Empty response from {url}, retry in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = [1, 3, 9][attempt]
                print(f"[{SITE_ID}] curl error: {exc}, retry in {wait}s")
                time.sleep(wait)
            else:
                print(f"[{SITE_ID}] curl failed after {retries} attempts: {url}: {exc}")
    return None


def _parse_date(date_str):
    """Convert M/D/YYYY to YYYY-MM-DD."""
    if not date_str:
        return None
    for fmt in ('%m/%d/%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime('%Y-%m-%d')
        except ValueError:
            pass
    return date_str.strip()


def _extract_pdf_abstract(pdf_bytes, max_pages=4):
    """Extract text from first N pages of a PDF as the abstract."""
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        text_parts = []
        for page in reader.pages[:max_pages]:
            try:
                text_parts.append(page.extract_text() or '')
            except Exception:
                pass
        raw = ' '.join(text_parts)
        # Collapse whitespace
        raw = re.sub(r'\s+', ' ', raw).strip()
        return raw
    except Exception as exc:
        print(f"[{SITE_ID}] pypdf failed, trying pdfminer: {exc}")

    try:
        from pdfminer.high_level import extract_text_to_fp
        from pdfminer.layout import LAParams
        out = io.StringIO()
        with io.BytesIO(pdf_bytes) as f:
            extract_text_to_fp(f, out, laparams=LAParams(), maxpages=max_pages)
        raw = re.sub(r'\s+', ' ', out.getvalue()).strip()
        return raw
    except Exception as exc2:
        print(f"[{SITE_ID}] pdfminer also failed: {exc2}")
        return ''


def _parse_library_records(html):
    """Extract records from the ld+json Dataset embedded in the library page."""
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL
    )
    for block in blocks:
        try:
            data = json.loads(block)
        except Exception:
            continue
        if data.get('@type') != 'Dataset':
            continue
        vars_ = {v['name']: v['value'] for v in data.get('variableMeasured', [])}
        ids = vars_.get('ID', [])
        dates = vars_.get('Effective Date', [])
        titles_html = vars_.get('Title', [])
        records = []
        for i, doc_id in enumerate(ids):
            date_raw = dates[i] if i < len(dates) else ''
            title_raw = unescape(titles_html[i]) if i < len(titles_html) else ''
            url_m = re.search(r'href=["\']([^"\']+)["\']', title_raw)
            media_url = url_m.group(1) if url_m else ''
            title_text = re.sub(r'<[^>]+>', '', title_raw).strip()
            media_id_m = re.search(r'/media/(\d+)', media_url)
            media_id = media_id_m.group(1) if media_id_m else None
            records.append({
                'doc_id': doc_id,
                'date': date_raw,
                'title': title_text,
                'media_url': media_url,
                'media_id': media_id,
            })
        return records
    return []


def _doc_type_from_id(doc_id):
    """Extract document type from DOE ID like 'DOE O 225.1B' → 'Order'."""
    m = re.match(r'DOE\s+([A-Z])\s+', doc_id or '')
    if m:
        return _TYPE_MAP.get(m.group(1), m.group(1))
    return ''


class DirectivesDoeGovDirectivesBrowseCrawler(BaseCrawler):

    site_id = SITE_ID
    site_name = "Custom: directives-doe-gov-directives-browse"
    base_url = "https://www.directives.doe.gov"

    def crawl(self, limit=None):
        start_time = time.time()
        MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

        saved = 0
        seen_urls = set()
        limit_str = str(limit) if limit is not None else 'inf'

        print(f"[{SITE_ID}] Starting crawl (limit={limit_str})")

        # Step 1: fetch the library page
        html = _curl_get(LIBRARY_URL)
        if not html:
            print(f"[{SITE_ID}] Failed to fetch library page, aborting.")
            return 0

        records = _parse_library_records(html)
        if not records:
            print(f"[{SITE_ID}] No records parsed from library page, aborting.")
            return 0

        print(f"[{SITE_ID}] Found {len(records)} directives in library.")

        # Step 2: iterate records
        for idx, rec in enumerate(records):
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time > MAX_WALL_SECONDS:
                print(f"[{SITE_ID}] Wall-clock budget reached at record {idx}, stopping.")
                break

            if idx % 10 == 0 and idx > 0:
                print(f"[{SITE_ID}] page 1: saved {saved}/{limit_str} (record {idx}/{len(records)})")

            media_url = rec['media_url']
            if not media_url:
                print(f"[{SITE_ID}] Record {idx} has no URL, skipping: {rec['doc_id']}")
                continue

            if media_url in seen_urls:
                continue
            seen_urls.add(media_url)

            try:
                time.sleep(self._delay)

                # Download PDF
                pdf_bytes = _curl_get(media_url, retries=3, binary=True)
                if not pdf_bytes:
                    print(f"[{SITE_ID}] item {media_url} failed: no PDF data")
                    continue

                # Check it's actually a PDF
                if not pdf_bytes[:5].startswith(b'%PDF-'):
                    print(f"[{SITE_ID}] item {media_url} failed: not a PDF ({pdf_bytes[:20]})")
                    continue

                # Extract abstract from PDF
                abstract = _extract_pdf_abstract(pdf_bytes, max_pages=4)
                if len(abstract) < 50:
                    print(f"[{SITE_ID}] Abstract too short ({len(abstract)} chars) for {rec['doc_id']}, skipping.")
                    continue

                doc_type = _doc_type_from_id(rec['doc_id'])
                pub_date = _parse_date(rec['date'])
                media_id = rec['media_id']

                # Build original filename from the URL path
                original_filename = media_url.rstrip('/').split('/')[-1]
                if not original_filename.endswith('.pdf'):
                    original_filename = None

                metadata = {
                    'doe_id': rec['doc_id'],
                    'doc_type': doc_type,
                    'effective_date_raw': rec['date'],
                    'media_id': media_id,
                }

                paper = {
                    'site_id': self.site_id,
                    'external_id': rec['doc_id'],
                    'post_number': media_id,
                    'title': rec['title'] or rec['doc_id'],
                    'abstract': abstract,
                    'url': media_url,
                    'pdf_url': media_url,
                    'published_date': pub_date,
                    'listed_date': pub_date,
                    'publisher': 'U.S. Department of Energy',
                    'category': doc_type,
                    'keywords': None,
                    'authors': None,
                    'department': None,
                    'doi': None,
                    'original_filename': original_filename,
                    'metadata': json.dumps(metadata, ensure_ascii=False),
                }

                self._save_paper(paper)
                saved += 1
                print(f"[{SITE_ID}] Saved [{saved}]: {rec['doc_id']} — {rec['title'][:50]}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{SITE_ID}] item {media_url} failed: {exc}")
                continue

        print(f"[{SITE_ID}] Done. Saved {saved} records.")
        return saved
