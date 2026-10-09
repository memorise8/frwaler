"""Enumerate the bounded ENSAM code query, then reconcile native SQLite saves.

The original title/abstract exclusions are reported, not endorsed as the user's
full collection policy. File downloads and institution-wide scope remain open.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
from urllib.parse import parse_qsl, urlsplit
from unittest.mock import patch

from crawler import db_libertree
from crawler.base_crawler import CrawlCancelled
from .diagnostics import curl_header_probe
from .request_gate import RequestGate, NetworkDeferred
from .results import atomic_json, utc_now
from .scaht_persistence import AuditStop

ROOT = Path(__file__).resolve().parents[2]
SITE = 'artsetmetiers-hal-science-search'


def classify_records(records):
    """Independent mapping of the reviewed adapter's identity/save predicate."""
    manifest = []
    seen_source, seen_keys, seen_urls = set(), set(), set()
    for doc in records:
        source_id = str(doc.get('docid') or '')
        hal_id = doc.get('halId_s') or ''
        key = hal_id or source_id
        url = doc.get('uri_s') or (f'https://hal.science/{hal_id}' if hal_id else '')
        if not source_id or source_id in seen_source or not key or key in seen_keys or not url or url in seen_urls:
            raise AuditStop('missing_or_colliding_source_identity')
        seen_source.add(source_id); seen_keys.add(key); seen_urls.add(url)
        titles, abstracts = doc.get('title_s') or [], doc.get('abstract_s') or []
        if not isinstance(titles, list) or not isinstance(abstracts, list):
            raise AuditStop('unexpected_title_or_abstract_schema')
        title = titles[0].strip() if titles else ''
        abstract = abstracts[0].strip() if abstracts else ''
        reason = 'missing_title' if not title else 'abstract_shorter_than_100_chars' if len(abstract) < 100 else None
        manifest.append({'source_docid': source_id, 'post_number': key, 'meta_url': url,
                         'code_save_eligible': reason is None, 'exclusion_reason': reason,
                         'source_record_sha256': hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()})
    return manifest


def run(output, gate_dir, contracts, boundary):
    if os.environ.get('LIBERTREE_DB_BACKEND', 'sqlite') != 'sqlite':
        raise ValueError('audit requires SQLite backend')
    os.environ['LIBERTREE_DB_BACKEND'] = 'sqlite'
    output = Path(output); output.mkdir(parents=True, exist_ok=False)
    contract = next(c for c in json.loads(Path(contracts).read_text()) if c['site_id'] == SITE)
    reviewed = json.loads(Path(boundary).read_text())
    expected_total = reviewed['source_reported_total']
    contract_hash = hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()
    if (reviewed['site_id'] != SITE or type(expected_total) is not int or not 0 < expected_total <= 500
            or reviewed.get('query_contract_sha256') != contract_hash
            or reviewed.get('boundary_state') != 'sampled_boundaries_accessible_not_full_enumeration'):
        raise ValueError('requires reviewed positive bounded query total')
    source_file = ROOT/'crawler/sites/custom'/f'{SITE}.py'
    spec = importlib.util.spec_from_file_location('reviewed_arts_native', source_file)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    gate = RequestGate(gate_dir, delay=1.5)
    report = {'site_id': SITE, 'started_at': utc_now(), 'state': 'running',
              'source_file_sha256': hashlib.sha256(source_file.read_bytes()).hexdigest(),
              'query_contract': contract, 'expected_api_total': expected_total,
              'scope': 'exact ENSAM code query; metadata and declared code exclusions only',
              'operating_database_touched': False, 'files_verified': False,
              'full_collection_verified': False, 'requests': [], 'phases': []}
    def save(): atomic_json(output/'audit.json', report)
    def stop(signum, frame): raise AuditStop('wall_budget_reached')
    previous = signal.signal(signal.SIGALRM, stop); signal.alarm(1800)
    original_run = subprocess.run
    expected_query = sorted((k, str(v)) for k, v in contract['query_pairs'] if k not in ('rows', 'start'))
    def command(argv, *args, **kwargs):
        if not isinstance(argv, (list, tuple)) or Path(argv[0]).name != 'curl' or kwargs.get('shell'):
            raise AuditStop('unreviewed_transport')
        urls = [v for v in argv if str(v).startswith(('http://', 'https://'))]
        if len(urls) != 1 or len(report['requests']) >= 64:
            raise AuditStop('request_contract_or_budget')
        url = urls[0]; parsed = urlsplit(url)
        query = sorted((k, v) for k, v in parse_qsl(parsed.query) if k not in ('rows', 'start'))
        if url.split('?')[0] != contract['endpoint'] or query != expected_query:
            raise AuditStop('query_changed_from_reviewed_contract')
        with gate.enter(url):
            result, statuses, retry_at = curl_header_probe(argv, original_run, args, kwargs)
            report['requests'].append({'at': utc_now(), 'http_statuses': statuses,
                                       'curl_exit': result.returncode})
            save()
            if retry_at is not None:
                gate.defer(url, retry_at)
                raise NetworkDeferred(parsed.hostname, retry_at)
            if result.returncode or not statuses or statuses[-1] != 200:
                raise AuditStop('source_http_or_transport_failure')
            return result
    baseline = {}
    save()
    try:
        with patch.object(subprocess, 'run', command):
            source_crawler = module.ArtsetmetiersHalScienceSearchCrawler(None)
            records, offset = [], 0
            while True:
                data = source_crawler._fetch_page(offset, module._ROWS)
                response = (data or {}).get('response') or {}
                docs = response.get('docs')
                if response.get('numFound') != expected_total or not isinstance(docs, list):
                    raise AuditStop('source_total_or_schema_changed')
                baseline[offset] = docs
                atomic_json(output/f'api-page-{offset}.json', data)
                if not docs:
                    break
                records.extend(docs)
                if len(records) > expected_total:
                    raise AuditStop('source_window_overlap_or_total_inconsistent')
                offset += len(docs)
            if len(records) != expected_total:
                raise AuditStop('source_enumeration_count_mismatch')
            manifest = classify_records(records)
            atomic_json(output/'manifest.json', manifest)
            expected = {r['post_number'] for r in manifest if r['code_save_eligible']}
            report.update(source_unique_count=len(manifest), code_eligible_count=len(expected),
                          excluded_by_code=len(manifest)-len(expected),
                          api_id_enumeration_complete_for_observation=True,
                          exclusion_policy_accepted=False)
            save()
            if len(expected) < 9:
                raise AuditStop('small_eligible_set_needs_adjusted_cancel_protocol')
            database = output/'audit.sqlite'
            for index, (limit, cancel_after) in enumerate(((3, None), (3, None), (None, 2), (None, None), (None, None)), 1):
                conn = sqlite3.connect(database)
                try:
                    db_libertree.init_db(conn); db_libertree.upsert_site(conn, SITE, SITE, 'https://artsetmetiers.hal.science')
                    count = lambda: conn.execute('SELECT count(*) FROM documents').fetchone()[0]
                    before = count(); prior = dict(conn.execute('SELECT post_number,seq_id FROM documents'))
                    phase = {'number': index, 'before': before, 'limit_new': limit, 'cancel_after': cancel_after, 'state': 'running'}
                    report['phases'].append(phase); save()
                    crawler = module.ArtsetmetiersHalScienceSearchCrawler(conn)
                    crawler.delivery_mode = 'incremental'
                    if cancel_after:
                        crawler.delivery_should_cancel = lambda: count() >= before + cancel_after
                    original_fetch = crawler._fetch_page
                    def fetch(start, rows):
                        data = original_fetch(start, rows)
                        response = (data or {}).get('response') or {}
                        if response.get('numFound') != expected_total or response.get('docs') != baseline.get(start):
                            raise AuditStop('source_records_changed_between_runs')
                        return data
                    crawler._fetch_page = fetch
                    try:
                        phase['returned_new'] = crawler.crawl(limit)
                        phase['state'] = 'returned'
                    except CrawlCancelled:
                        phase['state'] = 'cancelled'
                    finally:
                        if phase['state'] == 'running': phase['state'] = 'interrupted'
                        phase.update(after=count(), actual_new=count()-before,
                                     source_failure=crawler.delivery_source_failure,
                                     crawler_exhausted=crawler.delivery_exhausted)
                        save(); crawler._session.close()
                    after = dict(conn.execute('SELECT post_number,seq_id FROM documents'))
                    required_new = (3 if index <= 2 else 2 if index == 3 else len(expected)-before)
                    if phase['actual_new'] != required_new or phase['source_failure'] or any(after.get(k) != v for k,v in prior.items()):
                        raise AuditStop('save_count_or_identity_contract_failed')
                    if cancel_after and phase['state'] != 'cancelled':
                        raise AuditStop('cancel_not_observed')
                    if not cancel_after and phase.get('returned_new') != required_new:
                        raise AuditStop('reported_new_count_mismatch')
                    if index >= 4 and set(after) != expected:
                        raise AuditStop('saved_identity_set_mismatch')
                finally:
                    conn.close()
            report.update(state='enumerated_and_code_eligible_metadata_reconciled',
                          stored_unique_count=len(expected), missing_eligible_ids=[], unexpected_ids=[],
                          code_eligible_metadata_reconciled=True)
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
    for name in ('output', 'gate-dir', 'contracts', 'boundary'):
        parser.add_argument('--'+name, required=True)
    args = parser.parse_args()
    result = run(args.output, args.gate_dir, args.contracts, args.boundary)
    print(json.dumps({k: result.get(k) for k in ('state', 'source_unique_count', 'code_eligible_count')}))
    return 0 if result['state'] == 'enumerated_and_code_eligible_metadata_reconciled' else 1


if __name__ == '__main__':
    raise SystemExit(main())
