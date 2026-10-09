"""Resume an observed Federal Register cursor stream without trusting display totals.

The result covers the exact reviewed HHS API query as observed, not an immutable
snapshot, all agency material, actual database saves, or publisher files.
"""
import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import signal
from urllib.parse import urlencode, parse_qsl, urlsplit, urlunsplit

from crawler.sites import CRAWLERS
from .federal_persistence import SITE
from .request_gate import RequestGate, NetworkDeferred
from .results import atomic_json, utc_now
from .scaht_persistence import AuditStop
from .source_observer import fetch_once, retry_after


def summarize(records):
    items = []
    for doc in records:
        key = (doc.get('document_number') or '').strip()
        if not key: raise AuditStop('missing_source_identifier')
        title = (doc.get('title') or '').strip()
        abstract = max(len((doc.get('abstract') or '').strip()), len((doc.get('excerpts') or '').strip()))
        reason = 'missing_title' if not title else 'abstract_or_excerpt_shorter_than_50' if abstract < 50 else None
        items.append({'document_number': key, 'meta_url': doc.get('html_url'), 'pdf_url': doc.get('pdf_url'),
                      'publication_date': doc.get('publication_date'), 'code_save_eligible': reason is None,
                      'code_exclusion_reason': reason,
                      'record_sha256': hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()})
    return items


def run(output, gate_dir, *, resume=False, page_budget=100, fetch=fetch_once, date_range=None):
    if type(page_budget) is not int or not 1 <= page_budget <= 500:
        raise ValueError('page budget must be 1..500')
    output = Path(output); cls = CRAWLERS[SITE]; crawler = cls(None)
    source_hash = hashlib.sha256(Path(cls.crawl.__code__.co_filename).read_bytes()).hexdigest()
    pairs = [('conditions[agencies][]', cls._AGENCY_SLUG), ('conditions[search_type_id]', '3'),
             ('order', 'newest'), ('per_page', str(cls._PAGE_SIZE)), ('page', '1')]
    range_pairs = []
    if date_range is not None:
        if (set(date_range) != {'lower', 'upper', 'expected_count'}
                or type(date_range['expected_count']) is not int or not 0 <= date_range['expected_count'] < 1500
                or date.fromisoformat(date_range['lower']) > date.fromisoformat(date_range['upper'])):
            raise ValueError('requires a reviewed date range below the conservative count ceiling')
        range_pairs = [('conditions[publication_date][gte]', date_range['lower']),
                       ('conditions[publication_date][lte]', date_range['upper'])]
        pairs += range_pairs
    initial_url = cls._API_BASE+'?'+urlencode(pairs)
    crawler._session.close()
    def check_url(url):
        if date_range is None: return crawler._checked_next_url(url)
        parsed = urlsplit(url); query = parse_qsl(parsed.query, keep_blank_values=True)
        date_keys = {k for k, v in range_pairs}
        if sorted((k, v) for k, v in query if k in date_keys) != sorted(range_pairs):
            raise AuditStop('continuation_changes_date_range')
        crawler._checked_next_url(urlunsplit(parsed._replace(query=urlencode([(k, v) for k, v in query if k not in date_keys]))))
        return url
    def inspect_data(data, seen_count):
        if (not isinstance(data, dict) or not isinstance(data.get('results'), list)
                or (date_range is None and 'next_page_url' not in data)):
            raise AuditStop('source_schema_invalid')
        items = summarize(data['results'])
        if date_range is not None:
            if data.get('count') != date_range['expected_count']:
                raise AuditStop('date_range_count_changed')
            for item in items:
                published = date.fromisoformat(item['publication_date']).isoformat()
                if not date_range['lower'] <= published <= date_range['upper']:
                    raise AuditStop('date_filter_not_honored')
            if not data.get('next_page_url') and seen_count + len(items) != date_range['expected_count']:
                raise AuditStop('date_range_terminal_count_mismatch')
        return items
    manifest, seen_ids, seen_urls = [], set(), set()
    if resume:
        report = json.loads((output/'scope.json').read_text())
        if (report['state'] not in ('checkpointed', 'running') or report['source_file_sha256'] != source_hash
                or report['initial_url'] != initial_url or report.get('date_range') != date_range):
            raise ValueError('resume requires matching source/query and a resumable checkpoint')
        expected_url = initial_url
        for page in report['pages']:
            if page['request_url'] != expected_url: raise ValueError('checkpoint cursor chain mismatch')
            body = (output/page['evidence']).read_bytes()
            if hashlib.sha256(body).hexdigest() != page['body_sha256']: raise ValueError('checkpoint body hash mismatch')
            data = json.loads(body); items = inspect_data(data, len(seen_ids)); ids = [i['document_number'] for i in items]
            if len(ids) != len(set(ids)) or seen_ids.intersection(ids): raise ValueError('checkpoint repeated IDs')
            manifest.extend(items); seen_ids.update(ids); seen_urls.add(expected_url)
            expected_url = data.get('next_page_url')
            if expected_url: check_url(expected_url)
        if expected_url != report['next_url'] or len(seen_ids) != report['observed_unique_ids']:
            raise ValueError('checkpoint count or continuation mismatch')
    else:
        output.mkdir(parents=True, exist_ok=False)
        report = {'site_id': SITE, 'started_at': utc_now(), 'state': 'running', 'initial_url': initial_url,
                  'next_url': initial_url, 'source_file_sha256': source_hash, 'pages': [], 'runs': [],
                  'observed_unique_ids': 0, 'first_reported_count': None,
                  'date_range': date_range,
                  'scope': 'exact HHS code-query provider cursor stream, observed source IDs and code exclusions',
                  'full_collection_verified': False, 'immutable_snapshot_verified': False,
                  'exclusion_policy_accepted': False, 'database_persistence_verified': False, 'files_verified': False}
    run_info = {'started_at': utc_now(), 'page_budget': page_budget, 'resume': resume, 'pages_added': 0}
    report['runs'].append(run_info); report['state'] = 'running'
    gate = RequestGate(gate_dir, delay=1.5)
    def save():
        report['updated_at'] = utc_now(); atomic_json(output/'scope.json', report)
    def stop(signum, frame): raise AuditStop('wall_budget_reached')
    previous = signal.signal(signal.SIGALRM, stop); signal.alarm(3600); save()
    try:
        while report['next_url']:
            if run_info['pages_added'] >= page_budget:
                report['state'] = 'checkpointed'; break
            url = report['next_url']; check_url(url)
            if url in seen_urls: raise AuditStop('repeated_provider_cursor')
            with gate.enter(url):
                response = fetch(url, timeout=25)
                until = None
                if response['status'] == 429 or (response['status'] == 503 and response['headers'].get('retry-after')):
                    until = retry_after(response['headers'].get('retry-after')); gate.defer(url, until)
            body_hash = hashlib.sha256(response['body']).hexdigest()
            filename = f'page-{len(report["pages"])+1:05d}-{body_hash[:16]}.json'
            file = output/filename
            if file.exists() and hashlib.sha256(file.read_bytes()).hexdigest() != body_hash:
                raise AuditStop('existing_page_evidence_changed')
            if not file.exists(): file.write_bytes(response['body'])
            report['last_response'] = {'request_url': url, 'http_status': response['status'],
                                       'curl_exit': response['curl_exit'], 'body_complete': response['body_complete'],
                                       'evidence': filename, 'body_sha256': body_hash}
            save()
            if until is not None: raise NetworkDeferred('www.federalregister.gov', until)
            if response['status'] != 200 or response['curl_exit'] or not response['body_complete']:
                report['failed_request'] = {'http_status': response['status'], 'curl_exit': response['curl_exit']}
                raise AuditStop('source_http_or_body_incomplete')
            data = json.loads(response['body'])
            items = inspect_data(data, len(seen_ids)); ids = [item['document_number'] for item in items]
            if len(ids) != len(set(ids)) or seen_ids.intersection(ids): raise AuditStop('repeated_source_identifiers')
            next_url = data.get('next_page_url')
            if next_url is not None and not isinstance(next_url, str): raise AuditStop('invalid_provider_cursor')
            if next_url:
                check_url(next_url)
                if not items: raise AuditStop('empty_page_with_continuation')
                if next_url == url or next_url in seen_urls: raise AuditStop('repeated_provider_cursor')
            if report['first_reported_count'] is None: report['first_reported_count'] = data.get('count')
            # A wall-budget signal must not persist half of a page checkpoint.
            old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
            try:
                report['pages'].append({'number': len(report['pages'])+1, 'at': utc_now(), 'request_url': url,
                                        'reported_count': data.get('count'), 'records': len(items),
                                        'body_sha256': body_hash, 'evidence': filename})
                manifest.extend(items); seen_ids.update(ids); seen_urls.add(url)
                report.update(next_url=next_url, observed_unique_ids=len(seen_ids),
                              observed_code_eligible=sum(m['code_save_eligible'] for m in manifest))
                run_info['pages_added'] += 1; save()
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        else:
            reported = report['first_reported_count']
            if type(reported) is int and len(seen_ids) < reported:
                raise AuditStop('cursor_terminal_before_reported_lower_bound')
            atomic_json(output/'manifest.json', manifest)
            report.update(state='provider_cursor_terminal_observed',
                          source_id_stream_enumerated_for_observation=True,
                          manifest_sha256=hashlib.sha256((output/'manifest.json').read_bytes()).hexdigest())
    except NetworkDeferred as exc:
        report.update(state='host_cooldown_deferred', retry_at=exc.retry_at)
    except AuditStop as exc:
        report.update(state='checkpointed' if str(exc) == 'wall_budget_reached' else 'incomplete', reason=str(exc))
    except Exception as exc:
        report.update(state='incomplete', exception_type=type(exc).__name__)
    finally:
        signal.alarm(0); signal.signal(signal.SIGALRM, previous)
        run_info.update(finished_at=utc_now(), state=report['state']); save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True); parser.add_argument('--gate-dir', required=True)
    parser.add_argument('--resume', action='store_true'); parser.add_argument('--page-budget', type=int, default=100)
    args = parser.parse_args(); result = run(args.output, args.gate_dir, resume=args.resume, page_budget=args.page_budget)
    print(json.dumps({k: result.get(k) for k in ('state', 'observed_unique_ids', 'first_reported_count')}))
    return 0 if result['state'] in ('provider_cursor_terminal_observed', 'checkpointed') else 1


if __name__ == '__main__':
    raise SystemExit(main())
