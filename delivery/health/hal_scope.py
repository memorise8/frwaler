"""Enumerate unique source IDs for reviewed, uniquely ordered HAL code queries.

This proves an observed query ID set, not an immutable snapshot, intended whole
institution scope, save eligibility, database persistence, or file completeness.
"""
import argparse
import hashlib
import json
from pathlib import Path
import signal
import time
from urllib.parse import urlencode, urlsplit

from .request_gate import NetworkDeferred, RequestGate
from .results import atomic_json, utc_now
from .scaht_persistence import AuditStop
from .source_observer import fetch_once, retry_after


def enumerate_query(contract, output, gate, *, fetch=fetch_once, rows=200, max_total=50000):
    output = Path(output); output.mkdir(parents=True, exist_ok=False)
    endpoint = urlsplit(contract['endpoint'])
    pairs = [(k, v) for k, v in contract['query_pairs'] if k not in ('rows', 'start')]
    sorts = [v for k,v in pairs if k == 'sort']
    if (endpoint.scheme != 'https' or endpoint.netloc != 'api.archives-ouvertes.fr'
            or not endpoint.path.startswith('/search/') or endpoint.query or endpoint.fragment
            or len(sorts) != 1 or not any(p.strip() in ('docid asc', 'docid desc') for p in sorts[0].split(','))
            or not 1 <= rows <= 200):
        raise ValueError('requires reviewed uniquely ordered HAL API query')
    report = {'site_id': contract['site_id'], 'started_at': utc_now(), 'state': 'running',
              'contract': contract, 'query_contract_sha256': hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest(),
              'scope': 'observed exact code query ID enumeration', 'source_reported_total': None,
              'observed_unique_ids': 0, 'pages': [], 'full_collection_verified': False,
              'immutable_snapshot_verified': False, 'save_eligibility_verified': False,
              'database_persistence_verified': False, 'files_verified': False,
              'limits': {'rows': rows, 'total': max_total, 'wall_seconds': 1800}}
    def save():
        report['updated_at'] = utc_now(); atomic_json(output/'scope.json', report)
    seen = set(); manifest = []; start = 0; started = time.monotonic(); total = None
    def expired(signum, frame): raise AuditStop('wall_budget_reached')
    previous = signal.signal(signal.SIGALRM, expired); signal.alarm(1800)
    save()
    try:
        while True:
            if time.monotonic()-started > 1800: raise AuditStop('wall_budget_reached')
            url = contract['endpoint']+'?'+urlencode(pairs+[('rows', rows), ('start', start)])
            with gate.enter(url):
                response = fetch(url, timeout=25)
                if response['status'] == 429 or (response['status'] == 503 and response['headers'].get('retry-after')):
                    until = retry_after(response['headers'].get('retry-after')); gate.defer(url, until)
                    raise NetworkDeferred(endpoint.hostname, until)
            page = {'start': start, 'at': utc_now(), 'http_status': response['status'], 'curl_exit': response['curl_exit']}
            report['pages'].append(page)
            if response['status'] != 200 or response['curl_exit'] or not response['body_complete']:
                raise AuditStop('source_http_or_body_incomplete')
            data = json.loads(response['body']); result = data.get('response') or {}
            found, docs = result.get('numFound'), result.get('docs')
            if type(found) is not int or found < 0 or not isinstance(docs, list):
                raise AuditStop('source_schema_invalid')
            if total is None:
                total = found; report['source_reported_total'] = total
                if total > max_total: raise AuditStop('query_total_exceeds_reviewed_budget')
            if found != total: raise AuditStop('source_total_changed_during_enumeration')
            filename = f'page-{start}.json'; (output/filename).write_bytes(response['body'])
            page.update(records=len(docs), body_sha256=hashlib.sha256(response['body']).hexdigest(), evidence=filename)
            ids = [str(d.get('docid') or '') for d in docs]
            if any(not key for key in ids) or len(set(ids)) != len(ids) or seen.intersection(ids):
                raise AuditStop('missing_or_repeated_source_identifiers')
            if not docs:
                if len(seen) != total: raise AuditStop('terminal_page_before_reported_total')
                break
            if len(seen)+len(docs) > total: raise AuditStop('observed_rows_exceed_reported_total')
            for doc, key in zip(docs, ids):
                manifest.append({'docid': key, 'hal_id': doc.get('halId_s'), 'uri': doc.get('uri_s'),
                                 'record_sha256': hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()})
            seen.update(ids); start += len(docs); report['observed_unique_ids'] = len(seen); save()
        atomic_json(output/'manifest.json', manifest)
        report.update(state='observed_query_ids_enumerated', observed_unique_ids=len(seen),
                      manifest_sha256=hashlib.sha256((output/'manifest.json').read_bytes()).hexdigest(),
                      source_id_enumeration_complete_for_observation=True)
    except NetworkDeferred as exc:
        report.update(state='host_cooldown_deferred', retry_at=exc.retry_at)
    except AuditStop as exc:
        report.update(state='incomplete', reason=str(exc))
    except Exception as exc:
        report.update(state='incomplete', exception_type=type(exc).__name__)
    finally:
        signal.alarm(0); signal.signal(signal.SIGALRM, previous)
        report['finished_at'] = utc_now(); save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--contracts', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--gate-dir', type=Path, required=True)
    args = parser.parse_args(); args.output.mkdir(parents=True, exist_ok=False)
    gate = RequestGate(args.gate_dir, delay=1.5)
    contracts = json.loads(args.contracts.read_text())
    scan = {'started_at': utc_now(), 'finished_at': None, 'total': len(contracts), 'completed': 0,
            'outcomes': {}, 'scope': 'reviewed code query ID enumeration only', 'full_collection_verified': False}
    atomic_json(args.output/'scan.json', scan)
    for contract in contracts:
        result = enumerate_query(contract, args.output/contract['site_id'], gate)
        scan['outcomes'][contract['site_id']] = result['state']; scan['completed'] += 1
        scan['updated_at'] = utc_now(); atomic_json(args.output/'scan.json', scan)
        if (result['state'] == 'host_cooldown_deferred'
                or any(p['http_status'] in (401, 403) for p in result['pages'])): break
    scan['finished_at'] = utc_now(); atomic_json(args.output/'scan.json', scan)
    return 0 if scan['completed'] == scan['total'] and all(s == 'observed_query_ids_enumerated' for s in scan['outcomes'].values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
