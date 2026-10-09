"""Plan disjoint Federal Register date ranges below observed count ceilings.

Reported leaf totals are inventory candidates, not ID enumeration, actual saves,
immutable snapshots, or proof of the whole institution's collection scope.
"""
import argparse
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import signal
from urllib.parse import urlencode, parse_qs, urlsplit

from crawler.sites import CRAWLERS
from .federal_persistence import SITE
from .request_gate import RequestGate, NetworkDeferred
from .results import atomic_json, utc_now
from .scaht_persistence import AuditStop
from .source_observer import fetch_once, retry_after


def query_url(cls, *, order='newest', lower=None, upper=None):
    pairs = [('conditions[agencies][]', cls._AGENCY_SLUG), ('conditions[search_type_id]', '3'),
             ('order', order), ('per_page', '200'), ('page', '1')]
    if lower is not None: pairs.append(('conditions[publication_date][gte]', lower))
    if upper is not None: pairs.append(('conditions[publication_date][lte]', upper))
    return cls._API_BASE+'?'+urlencode(pairs)


def partition(lower, upper):
    lo, hi = date.fromisoformat(lower), date.fromisoformat(upper)
    if hi <= lo: raise AuditStop('single_day_exceeds_leaf_budget')
    mid = lo + (hi-lo)//2
    return [{'lower': lo.isoformat(), 'upper': mid.isoformat()},
            {'lower': (mid+timedelta(days=1)).isoformat(), 'upper': hi.isoformat()}]


def run(output, gate_dir, *, resume=False, request_budget=200, leaf_limit=1500, fetch=fetch_once):
    if type(request_budget) is not int or not 1 <= request_budget <= 500:
        raise ValueError('request budget must be 1..500')
    if type(leaf_limit) is not int or not 2 <= leaf_limit <= 2000:
        raise ValueError('leaf count ceiling must be 2..2000')
    output = Path(output); cls = CRAWLERS[SITE]
    digest = hashlib.sha256(Path(cls.crawl.__code__.co_filename).read_bytes())
    digest.update(Path(__file__).read_bytes()); source_hash = digest.hexdigest()
    if resume:
        report = json.loads((output/'plan.json').read_text())
        if (report['state'] not in ('running', 'checkpointed') or report['source_hash'] != source_hash
                or report['leaf_limit'] != leaf_limit):
            raise ValueError('requires matching resumable source and range policy')
        for response in report['responses']:
            if hashlib.sha256((output/response['evidence']).read_bytes()).hexdigest() != response['body_sha256']:
                raise ValueError('saved response hash mismatch')
    else:
        output.mkdir(parents=True, exist_ok=False)
        report = {'site_id': SITE, 'started_at': utc_now(), 'state': 'running', 'source_hash': source_hash,
                  'leaf_limit': leaf_limit, 'bounds': {}, 'pending': [], 'nodes': [], 'leaves': [], 'responses': [], 'runs': [],
                  'scope': 'original HHS/search_type_id membership; oldest/newest observations determine date bounds; disjoint inclusive date ranges',
                  'source_id_enumeration_verified': False, 'immutable_snapshot_verified': False,
                  'database_persistence_verified': False, 'files_verified': False, 'full_collection_verified': False,
                  'undated_source_records_verified': False,
                  'official_range_guidance': 'https://www.federalregister.gov/reader-aids/developer-resources/rest-api'}
    run_info = {'started_at': utc_now(), 'resume': resume, 'request_budget': request_budget, 'requests_added': 0}
    report['runs'].append(run_info); report['state'] = 'running'
    gate = RequestGate(gate_dir, delay=1.5)
    def save(): report['updated_at'] = utc_now(); atomic_json(output/'plan.json', report)
    def expired(s, f): raise AuditStop('wall_budget_reached')
    previous = signal.signal(signal.SIGALRM, expired); signal.alarm(3600)

    def read(url, lower=None, upper=None):
        if run_info['requests_added'] >= request_budget: raise AuditStop('request_budget_reached')
        with gate.enter(url):
            response = fetch(url, timeout=25)
            until = None
            if response['status'] == 429 or (response['status'] == 503 and response['headers'].get('retry-after')):
                until = retry_after(response['headers'].get('retry-after')); gate.defer(url, until)
        body_hash = hashlib.sha256(response['body']).hexdigest()
        filename = f'response-{len(report["responses"])+1:04d}-{body_hash[:16]}.json'
        (output/filename).write_bytes(response['body'])
        item = {'request_url': url, 'observed_at': utc_now(), 'http_status': response['status'],
                'curl_exit': response['curl_exit'], 'body_complete': response['body_complete'],
                'body_sha256': body_hash, 'evidence': filename}
        report['responses'].append(item); run_info['requests_added'] += 1; save()
        if until is not None: raise NetworkDeferred('www.federalregister.gov', until)
        if response['status'] != 200 or response['curl_exit'] or not response['body_complete']:
            raise AuditStop('source_http_or_body_incomplete')
        data = json.loads(response['body'])
        if not isinstance(data, dict) or not isinstance(data.get('results'), list) or type(data.get('count')) is not int:
            raise AuditStop('source_schema_invalid')
        docs, count = data['results'], data['count']
        if (count < 0 or (count == 0 and docs)
                or (count > 0 and not 1 <= len(docs) <= min(200, count))):
            raise AuditStop('source_count_or_sample_inconsistent')
        dates = []
        for doc in docs:
            if not isinstance(doc, dict) or not doc.get('document_number'): raise AuditStop('source_identity_missing')
            published = date.fromisoformat(doc['publication_date']).isoformat()
            dates.append(published)
            if (lower and published < lower) or (upper and published > upper):
                raise AuditStop('date_filter_not_honored')
        descending = parse_qs(urlsplit(url).query)['order'] == ['newest']
        if dates != sorted(dates, reverse=descending): raise AuditStop('date_order_not_honored')
        return data, filename

    save()
    try:
        for order in ('oldest', 'newest'):
            if order not in report['bounds']:
                data, filename = read(query_url(cls, order=order))
                if not data['results']: raise AuditStop('empty_query_requires_separate_empty_scope_review')
                record = data['results'][0]
                report['bounds'][order] = {'publication_date': record['publication_date'], 'document_number': record['document_number'],
                                           'reported_count': data['count'], 'evidence': filename}
                save()
        lower = report['bounds']['oldest']['publication_date']; upper = report['bounds']['newest']['publication_date']
        if lower > upper: raise AuditStop('inconsistent_date_bounds')
        if not report['nodes'] and not report['pending']:
            report['pending'] = [{'lower': lower, 'upper': upper}]; save()
        while report['pending']:
            node = report['pending'][0]
            data, filename = read(query_url(cls, **node), **node)
            count = data['count']
            children = partition(**node) if count >= leaf_limit else []
            complete = dict(node, reported_count=count, evidence=filename, state='split' if children else 'leaf_count_observed')
            old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
            try:
                report['nodes'].append(complete)
                report['pending'] = children + report['pending'][1:]
                if not children: report['leaves'].append(complete)
                report['reported_leaf_count_sum'] = sum(r['reported_count'] for r in report['leaves'])
                save()
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        leaves = sorted(report['leaves'], key=lambda r: r['lower'])
        if leaves[0]['lower'] != lower or leaves[-1]['upper'] != upper:
            raise AuditStop('partition_coverage_gap')
        for first, second in zip(leaves, leaves[1:]):
            if date.fromisoformat(first['upper']) + timedelta(days=1) != date.fromisoformat(second['lower']):
                raise AuditStop('partition_gap_or_overlap')
        report.update(state='disjoint_date_range_counts_observed', range_coverage_verified=True,
                      count_quality='sum of disjoint leaf reported counts below conservative ceiling; IDs and date-bound coverage still require enumeration')
    except NetworkDeferred as exc:
        report.update(state='host_cooldown_deferred', retry_at=exc.retry_at)
    except AuditStop as exc:
        report.update(state='checkpointed' if str(exc) in ('wall_budget_reached', 'request_budget_reached') else 'incomplete', reason=str(exc))
    except Exception as exc:
        report.update(state='incomplete', exception_type=type(exc).__name__)
    finally:
        signal.alarm(0); signal.signal(signal.SIGALRM, previous)
        run_info.update(finished_at=utc_now(), state=report['state']); save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True); parser.add_argument('--gate-dir', required=True)
    parser.add_argument('--resume', action='store_true'); parser.add_argument('--request-budget', type=int, default=200)
    args = parser.parse_args(); report = run(args.output, args.gate_dir, resume=args.resume, request_budget=args.request_budget)
    print(json.dumps({k: report.get(k) for k in ('state', 'reported_leaf_count_sum', 'reason')}))
    return 0 if report['state'] in ('disjoint_date_range_counts_observed', 'checkpointed') else 1


if __name__ == '__main__':
    raise SystemExit(main())
