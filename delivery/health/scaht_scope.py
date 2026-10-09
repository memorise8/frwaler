"""Enumerate the reviewed English SCAHT publication list, without DB writes.

Two matching traversals are observation evidence, not an immutable snapshot or
proof that publisher attachments and other languages have been collected.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from .request_gate import NetworkDeferred, RequestGate
from .results import atomic_json, utc_now
from .source_observer import fetch_once, retry_after

ENTRY = 'https://www.scaht.org/en/publications/'
HOSTS = {'www.scaht.org', 'scaht.org'}


def parse_listing(body, page):
    soup = BeautifulSoup(body, 'html.parser')
    records = []
    for item in soup.select('.publications-item'):
        anchors = item.select('a[href]')
        if len(anchors) != 1 or not anchors[0].get_text(' ', strip=True):
            raise ValueError('invalid_publication_link')
        anchor = anchors[0]
        url = urljoin(ENTRY, anchor['href'])
        u = urlsplit(url)
        match = re.fullmatch(r'/en/publications/([^/]+)/?', u.path)
        if (u.scheme not in {'http', 'https'} or u.hostname not in HOSTS
                or u.username or u.password or u.port or u.query or u.fragment or not match):
            raise ValueError('publication_outside_reviewed_scope')
        date = item.select_one('.publications-date')
        records.append({'external_id': match[1], 'url': url,
                        'title': anchor.get_text(' ', strip=True),
                        'date_raw': date.get_text(' ', strip=True) if date else None})
    ids = [r['external_id'] for r in records]
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate_identifiers_on_page')
    next_pages, linked_pages = [], []
    for anchor in soup.select('ul.pagination a.page-link[href]'):
        u = urlsplit(urljoin(ENTRY, anchor['href']))
        query = parse_qs(u.query, keep_blank_values=True)
        if (u.scheme != 'https' or u.hostname != 'www.scaht.org' or u.path != '/en/publications/'
                or u.username or u.password or u.port or u.fragment or set(query) != {'page'}
                or len(query['page']) != 1 or not query['page'][0].isdigit()):
            raise ValueError('pagination_outside_reviewed_scope')
        number = int(query['page'][0])
        if number < 1:
            raise ValueError('invalid_page_number')
        linked_pages.append(number)
        if anchor.get_text(' ', strip=True) == '»':
            next_pages.append(number)
    if len(next_pages) > 1 or (next_pages and next_pages[0] != page + 1):
        raise ValueError('nonadvancing_next_page')
    if not next_pages and any(n > page for n in linked_pages):
        raise ValueError('missing_next_with_later_pages')
    current = soup.select_one('ul.pagination .active .page-link')
    current_number = re.match(r'^\s*(\d+)', current.get_text() if current else '')
    if records and (not current_number or int(current_number[1]) != page):
        raise ValueError('current_page_not_confirmed')
    return {'records': records, 'next_page': next_pages[0] if next_pages else None,
            'active_page': int(current_number[1]) if current_number else None}


def enumerate_scope(output, gate_dir, *, max_pages=200):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    gate = RequestGate(gate_dir, delay=1.5)
    report = {'site_ids': ['scaht-org', 'scaht-org-en'], 'scope_id': 'scaht-en-publications',
              'entry_url': ENTRY, 'entity_unit': 'SCAHT English publication detail slug',
              'started_at': utc_now(), 'state': 'running', 'database_writes': False,
              'source_reported_total': None, 'enumerable_total': None,
              'full_collection_verified': False, 'snapshot_guarantee': False,
              'source_filters': 'English publication list, all visible pages; no date/type filter',
              'passes': [], 'requests': [], 'max_pages': max_pages}
    def save():
        atomic_json(output/'scope.json', report)
    def fetch(page, label):
        url = ENTRY + '?page=' + str(page)
        with gate.enter(url):
            response = fetch_once(url)
            if response['status'] == 429:
                until = retry_after(response['headers'].get('retry-after'))
                gate.defer(url, until)
                raise NetworkDeferred('www.scaht.org', until)
        body = response.pop('body')
        response['observed_at'] = utc_now()
        response['page'] = page
        response['label'] = label
        response['body_sha256'] = hashlib.sha256(body).hexdigest()
        report['requests'].append(response)
        save()
        if response['status'] != 200 or not response['body_complete']:
            raise ValueError('list_response_not_complete_http_200')
        (output/(label + '.html')).write_bytes(body)
        return parse_listing(body, page)
    save()
    try:
        for pass_number in (1, 2):
            traversal = {'number': pass_number, 'pages': [], 'records': [], 'overlapping_ids': []}
            report['passes'].append(traversal)
            seen = set()
            fingerprints = set()
            page = 1
            while True:
                if page > max_pages:
                    raise ValueError('page_budget_reached_not_exhausted')
                parsed = fetch(page, f'pass-{pass_number}-page-{page}')
                records = parsed['records']
                if not records:
                    raise ValueError('empty_page_before_explicit_end')
                ids = {r['external_id'] for r in records}
                fingerprint = tuple(sorted(ids))
                if fingerprint in fingerprints:
                    raise ValueError('repeated_page')
                fingerprints.add(fingerprint)
                traversal['overlapping_ids'].extend(sorted(ids & seen))
                seen.update(ids)
                traversal['pages'].append({'page': page, 'ids': sorted(ids),
                                           'next_page': parsed['next_page']})
                traversal['records'].extend(records)
                save()
                if parsed['next_page'] is None:
                    after = fetch(page + 1, f'pass-{pass_number}-after-last')
                    if after['records'] or after['next_page'] is not None:
                        raise ValueError('after_last_not_empty')
                    traversal['end_evidence'] = 'last_nonempty_has_no_next_and_following_page_empty'
                    break
                page = parsed['next_page']
        first, second = report['passes']
        if first['pages'] != second['pages'] or first['records'] != second['records']:
            raise ValueError('source_changed_between_complete_traversals')
        by_id = {}
        for record in first['records']:
            previous = by_id.get(record['external_id'])
            if previous is not None and previous != record:
                raise ValueError('same_id_has_conflicting_metadata')
            by_id[record['external_id']] = record
        manifest = sorted(by_id.values(), key=lambda r: r['external_id'])
        atomic_json(output/'manifest.json', manifest)
        overlap = bool(first['overlapping_ids'])
        report.update(state='matching_traversals_with_overlap' if overlap else 'two_matching_traversals_observed',
                      enumerable_total=None if overlap else len(manifest), observed_unique_count=len(manifest),
                      list_occurrences=len(first['records']),
                      count_quality='lower_bound_pagination_overlap_requires_review' if overlap else 'observed_unique_ids_in_declared_scope',
                      manifest_sha256=hashlib.sha256((output/'manifest.json').read_bytes()).hexdigest())
    except NetworkDeferred as exc:
        report.update(state='host_cooldown_deferred', retry_at=exc.retry_at)
    except Exception as exc:
        report.update(state='incomplete', failure_type=type(exc).__name__,
                      failure_reason=str(exc) if isinstance(exc, ValueError) else 'request_or_io_failure')
    finally:
        report['finished_at'] = utc_now()
        save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--gate-dir', required=True)
    args = parser.parse_args()
    result = enumerate_scope(args.output, args.gate_dir)
    print(json.dumps({k: result[k] for k in ('state', 'enumerable_total', 'full_collection_verified')}))
    return 0 if result['state'] == 'two_matching_traversals_observed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
