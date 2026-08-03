# -*- coding: utf-8 -*-
"""U.S. Department of State - Department Reports crawler.

Source: https://www.state.gov/department-reports/
API:    https://www.state.gov/wp-json/wp/v2/state_report  (WordPress REST API)
"""

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, '.')
from crawler.base_crawler import BaseCrawler

_API_BASE = "https://www.state.gov/wp-json/wp/v2"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_MONTHS = {
    'january': '01', 'february': '02', 'march': '03', 'april': '04',
    'may': '05', 'june': '06', 'july': '07', 'august': '08',
    'september': '09', 'october': '10', 'november': '11', 'december': '12',
}


def _curl_get(url, timeout=30):
    """GET via curl with retry; returns response body str or None."""
    cmd = [
        "curl", "-sL", "--max-time", str(timeout),
        "-H", f"User-Agent: {_UA}",
        "-H", "Accept: application/json",
        url,
    ]
    for attempt in range(3):
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=timeout + 10, errors='replace'
            )
            text = result.stdout.decode('utf-8', errors='replace') if isinstance(result.stdout, bytes) else result.stdout
            if text and text.strip():
                return text
        except Exception as exc:
            print(f"[state-gov-department-reports] curl error (attempt {attempt+1}/3): {exc}")
        if attempt < 2:
            wait = (attempt + 1) ** 3  # 1s, 8s
            time.sleep(wait)
    return None


def _strip_html(html):
    """Strip HTML tags, decode common entities, normalise whitespace."""
    if not html:
        return ""
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&quot;', '"', text)
    text = re.sub(r'&[a-zA-Z]+;', '', text)
    return re.sub(r'\s+', ' ', text).strip()


def _parse_doc_date(raw):
    """Parse "May 1, 2026" or ISO prefix to YYYY-MM-DD."""
    if not raw:
        return None
    raw = raw.strip()
    # ISO datetime: "2026-04-28T13:29:14" → "2026-04-28"
    if len(raw) >= 10 and raw[4] == '-':
        return raw[:10]
    # "Month D, YYYY" or "Month DD, YYYY"
    m = re.match(r'([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})', raw)
    if m:
        month = _MONTHS.get(m.group(1).lower(), '01')
        day = m.group(2).zfill(2)
        year = m.group(3)
        return f"{year}-{month}-{day}"
    return None


def _fetch_taxonomy_map(tax_type):
    """Fetch all terms for a WP taxonomy; return {id: name}."""
    result = {}
    page = 1
    while True:
        url = f"{_API_BASE}/{tax_type}?per_page=100&page={page}"
        raw = _curl_get(url)
        if not raw:
            break
        try:
            items = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            break
        if not isinstance(items, list) or not items:
            break
        for item in items:
            if isinstance(item, dict):
                result[item.get('id')] = item.get('name', '')
        if len(items) < 100:
            break
        page += 1
    return result


class StateGovDepartmentReportsCrawler(BaseCrawler):
    """Crawler for U.S. Department of State Department Reports."""

    site_id = "state-gov-department-reports"
    site_name = "Custom: state-gov-department-reports"
    base_url = "https://www.state.gov"

    _PER_PAGE = 100
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))

    def crawl(self, limit=None):
        """Crawl state.gov department reports via WordPress REST API.

        All report data (title, abstract, dates) is present in the list
        response — no per-item detail fetch is required.
        """
        start_time = time.time()
        max_seconds = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

        # Pre-fetch taxonomy term maps for human-readable names
        print(f"[{self.site_id}] Fetching taxonomy maps...")
        report_types_map = _fetch_taxonomy_map("state_report_types")
        subjects_map = _fetch_taxonomy_map("state_subjects")
        print(
            f"[{self.site_id}] Taxonomies loaded: "
            f"{len(report_types_map)} report types, {len(subjects_map)} subjects"
        )

        saved = 0
        seen_urls = set()
        page = 1
        limit_str = str(limit) if limit is not None else "∞"

        while True:
            # Wall-clock budget
            if time.time() - start_time > max_seconds:
                print(f"[{self.site_id}] 25-minute budget reached, exiting cleanly.")
                break

            if limit is not None and saved >= limit:
                break

            if page > self._MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached, stopping.")
                break

            # Progress log every 10 pages
            if page > 1 and page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            url = f"{_API_BASE}/state_report?per_page={self._PER_PAGE}&page={page}"

            # Fetch page with retry (exponential backoff)
            raw = None
            for attempt in range(3):
                raw = _curl_get(url)
                if raw:
                    break
                wait = (attempt + 1) ** 3
                print(
                    f"[{self.site_id}] Page {page} fetch failed "
                    f"(attempt {attempt+1}/3), retrying in {wait}s..."
                )
                time.sleep(wait)

            if not raw:
                print(f"[{self.site_id}] Page {page} failed after 3 retries. Stopping.")
                break

            try:
                items = json.loads(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                print(f"[{self.site_id}] Page {page} JSON decode error: {exc}. Stopping.")
                break

            if not isinstance(items, list):
                print(f"[{self.site_id}] Page {page} unexpected response type. Stopping.")
                break

            if not items:
                print(f"[{self.site_id}] Page {page} returned 0 items. Done.")
                break

            for item in items:
                if limit is not None and saved >= limit:
                    break

                try:
                    item_url = item.get('link', '') or ''

                    # URL deduplication — guards against paginator looping
                    if item_url in seen_urls:
                        continue
                    seen_urls.add(item_url)

                    post_id = item.get('id')
                    title = _strip_html(item.get('title', {}).get('rendered', '') or '')

                    acf = item.get('acf') or {}
                    gc = acf.get('group_country_content') or {}
                    gh = acf.get('group_global_header') or {}

                    # Abstract: per-country executive summary first
                    wys = gc.get('wys_executive_summary', '') or ''
                    abstract = _strip_html(wys)

                    # Fallback: report-level headline
                    if len(abstract) < 50:
                        headline = ''
                        if isinstance(gh, dict):
                            headline = gh.get('ta_global_headline', '') or ''
                        if isinstance(headline, str) and headline:
                            abstract = _strip_html(headline)

                    # Fallback: parse meaningful paragraphs from HTML content
                    if len(abstract) < 50:
                        content_html = item.get('content', {}).get('rendered', '') or ''
                        paras = re.findall(r'<p[^>]*>(.*?)</p>', content_html, re.DOTALL)
                        parts = []
                        for p in paras:
                            t = _strip_html(p)
                            if len(t) > 80:
                                parts.append(t)
                            if sum(len(x) for x in parts) >= 500:
                                break
                        abstract = ' '.join(parts)

                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {post_id} ({title[:40]}) "
                            f"abstract too short ({len(abstract)} chars), skipping."
                        )
                        continue

                    # Dates
                    listed_date = (item.get('date', '') or '')[:10] or None
                    doc_date_raw = acf.get('document_date', '') or ''
                    published_date = _parse_doc_date(doc_date_raw) or listed_date

                    # Taxonomy → human-readable names
                    rt_ids = item.get('state_report_types') or []
                    category = '; '.join(
                        report_types_map.get(i, str(i)) for i in rt_ids if i
                    )

                    subj_ids = item.get('state_subjects') or []
                    keywords = ', '.join(
                        subjects_map.get(i, str(i)) for i in subj_ids if i
                    )

                    year_ids = item.get('state_years') or []
                    policy_issue_ids = item.get('state_policy_issues') or []

                    # Parent report series title
                    parent_post = acf.get('parent_post_url_id')
                    series_title = ''
                    if isinstance(parent_post, dict):
                        series_title = parent_post.get('post_title', '') or ''

                    summary_title = gc.get('txt_executive_summary_title', '') or ''

                    paper = {
                        'site_id': self.site_id,
                        'external_id': str(post_id),
                        'post_number': str(post_id),
                        'title': title,
                        'abstract': abstract,
                        'published_date': published_date,
                        'posted_date': listed_date,
                        'url': item_url,
                        'pdf_url': None,
                        'doi': None,
                        'authors': '',
                        'publisher': 'U.S. Department of State',
                        'department': '',
                        'journal': series_title or None,
                        'category': category,
                        'keywords': keywords,
                        'original_filename': None,
                        'metadata': json.dumps({
                            'posted_date': item.get('date', ''),
                            'wordpress_id': post_id,
                            'slug': item.get('slug', ''),
                            'modified': item.get('modified', ''),
                            'document_date_raw': doc_date_raw,
                            'series': series_title,
                            'summary_title': summary_title,
                            'report_type_ids': rt_ids,
                            'subject_ids': subj_ids,
                            'year_ids': year_ids,
                            'policy_issue_ids': policy_issue_ids,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item.get('id', '?')} failed: {exc}")
                    continue

            # Rate-limit between pages
            time.sleep(self._delay)
            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
