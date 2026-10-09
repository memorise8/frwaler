"""Review query-specific API totals and first/middle/last boundaries, not full coverage.

Contracts are explicitly reviewed input, not inferred from arbitrary count fields.
Never equate the API's total with the number the crawler will actually save.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from .results import atomic_json, site_path, utc_now
from .scope_inventory import public_url
from .source_observer import HostGate, HostDeferred, fetch_once


def parse_page(kind, body):
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError('invalid_object')
    if kind == 'hal_solr':
        total, records = data['response']['numFound'], data['response']['docs']
        keys = [r.get('docid') for r in records] if isinstance(records, list) else []
    elif kind == 'doaj':
        total, records = data['total'], data['results']
        keys = [r.get('id') for r in records] if isinstance(records, list) else []
    elif kind == 'ots':
        raw_total, records = data['meta']['totalCount'], data['result']
        if type(raw_total) is not int and not (isinstance(raw_total, str) and raw_total.isdecimal()):
            raise ValueError('invalid_ots_total')
        total = int(raw_total)
        if data.get('error') or not isinstance(records, list) or data['meta']['count'] != len(records):
            raise ValueError('ots_error_or_count_mismatch')
        keys = [r.get('key') for r in records]
    else:
        raise ValueError('unreviewed_response_schema')
    if isinstance(total, bool) or not isinstance(total, int) or total < 0 or not isinstance(records, list):
        raise ValueError('invalid_count_or_records')
    if any(key is None for key in keys) or len(set(map(str, keys))) != len(keys):
        raise ValueError('missing_or_duplicate_page_ids')
    return {'total': total, 'records': len(records),
            'id_hashes': [hashlib.sha256(str(k).encode()).hexdigest() for k in keys],
            'body_sha256': hashlib.sha256(body).hexdigest()}


def query_url(contract, offset):
    kind = contract['kind']
    params = [(k, v) for k, v in contract['query_pairs']
              if k not in ('rows', 'start', 'pageSize', 'page', 'maxCountPerPage')]
    if kind == 'hal_solr':
        params += [('rows', 1), ('start', offset)]
    elif kind == 'doaj':
        # Real crawler's page size; page 1 corresponds to offset 0.
        params += [('pageSize', 50), ('page', offset // 50 + 1)]
    elif kind == 'ots':
        params += [('maxCountPerPage', 1), ('page', offset + 1)]
    else:
        raise ValueError('unreviewed_pagination_schema')
    url = contract['endpoint'] + '?' + urlencode(params)
    if not public_url(url):
        raise ValueError('non_public_contract')
    return url


def probe(contract, gate, *, fetch=fetch_once):
    result = {'site_id': contract['site_id'], 'observed_at': utc_now(),
              'scope': 'reviewed_code_query_not_all_public_site_documents',
              'query_contract_sha256': hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest(),
              'post_fetch_filter': contract.get('post_fetch_filter'),
              'source_reported_total': None, 'source_total_quality': 'unknown',
              'enumerable_total': None, 'full_id_enumeration': 'not_performed',
              'save_eligible_total': None, 'full_collection_verified': False,
              'boundary_checks': [], 'boundary_state': 'not_started'}
    if contract['kind'] not in ('hal_solr', 'doaj', 'ots'):
        result['boundary_state'] = 'response_schema_requires_review'
        return result

    def request(offset, label):
        url = query_url(contract, offset)
        host = urlsplit(url).hostname
        row = {'label': label, 'offset': offset, 'url': url}
        result['boundary_checks'].append(row)
        try:
            with gate.enter(host):
                response = fetch(url, timeout=20)
                if response['status'] == 429 or (response['status'] == 503 and response['headers'].get('retry-after')):
                    gate.defer(host, response['headers'].get('retry-after'))
            row.update(http_status=response['status'], curl_exit=response['curl_exit'])
            if response['curl_exit'] or response['status'] != 200 or not response['body_complete']:
                row['state'] = 'request_failed_or_incomplete'
                return None
            page = parse_page(contract['kind'], response['body'])
            if contract['kind'] == 'ots':
                data = json.loads(response['body'])
                expected_emittent = dict(contract['query_pairs']).get('emittentId')
                if (expected_emittent is None or str(data['meta'].get('emittentId')) != str(expected_emittent)
                        or any(str(r.get('emittentId')) != str(expected_emittent) for r in data['result'])
                        or str(data['meta'].get('page')) != str(offset + 1)):
                    raise ValueError('ots_scope_or_page_mismatch')
            row.update(state='parsed', **page)
            return page
        except HostDeferred:
            row['state'] = 'host_cooldown_deferred'
        except Exception as exc:
            row.update(state='parse_or_transport_error', exception_type=type(exc).__name__)
        return None

    first = request(0, 'first')
    if first is None:
        result['boundary_state'] = 'first_page_unverified'
        return result
    total = first['total']
    result.update(source_reported_total=total, source_total_quality='api_reported_for_reviewed_query')
    page_size = 50 if contract['kind'] == 'doaj' else 1
    if first['records'] != min(page_size, total):
        result['boundary_state'] = 'first_count_inconsistent'
        return result
    if total == 0:
        result['boundary_state'] = 'api_reported_empty_query_not_entire_site'
        return result
    last = ((total-1) // page_size) * page_size
    middle = ((total//2) // page_size) * page_size
    after = ((total+page_size-1) // page_size) * page_size
    offsets = {}
    for label, offset in [('middle', middle), ('last', last), ('after_last', after)]:
        if offset == 0:
            continue
        offsets.setdefault(offset, []).append(label)
    seen = set(first['id_hashes'])
    for offset, labels in offsets.items():
        page = request(offset, '+'.join(labels))
        if page is None:
            result['boundary_state'] = 'boundary_access_unverified'
            return result
        if page['total'] != total:
            result['boundary_state'] = ('terminal_page_resets_reported_total_requires_review'
                                       if contract['kind'] == 'ots' and offset == after
                                       and page['total'] == 0 and page['records'] == 0
                                       else 'source_changed_between_observations')
            return result
        expected = max(0, min(page_size, total-offset))
        if page['records'] != expected:
            result['boundary_state'] = 'page_count_or_end_inconsistent'
            return result
        if seen.intersection(page['id_hashes']):
            result['boundary_state'] = 'repeated_ids_between_distinct_pages'
            return result
        seen.update(page['id_hashes'])
    result['boundary_state'] = 'sampled_boundaries_accessible_not_full_enumeration'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--contracts', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(mode=0o700, parents=True, exist_ok=False)
    gate = HostGate()
    contracts = json.loads(args.contracts.read_text())
    results = []
    state = {'started_at': utc_now(), 'total': len(contracts), 'completed': 0, 'finished_at': None}
    atomic_json(args.out/'scan.json', state)
    for contract in contracts:
        result = probe(contract, gate)
        atomic_json(site_path(args.out, contract['site_id']), result)
        results.append(result)
        state.update(completed=len(results), updated_at=utc_now())
        atomic_json(args.out/'scan.json', state)
    state.update(finished_at=utc_now(), outcomes={r['site_id']: r['boundary_state'] for r in results},
                 host_cooldowns=gate.cooldowns())
    atomic_json(args.out/'scan.json', state)
    print(json.dumps(state))


if __name__ == '__main__':
    main()
