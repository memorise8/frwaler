"""Native Federal Register first-window persistence pilot, not full collection."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
from urllib.parse import urlsplit

from crawler import db_libertree
from crawler.base_crawler import CrawlCancelled
from crawler.sites import CRAWLERS
from .file_audit import AuditSession
from .request_gate import NetworkDeferred, RequestGate
from .results import atomic_json, utc_now
from .scaht_persistence import AuditStop

SITE = 'federalregister-gov-documents'


def eligible_ids(records):
    ids = []
    for item in records:
        title = (item.get('title') or '').strip()
        abstract = (item.get('abstract') or '').strip()
        if len(abstract) < 50:
            excerpt = (item.get('excerpts') or '').strip()
            if len(excerpt) > len(abstract): abstract = excerpt
        if not title or len(abstract) < 50: continue
        key, url = (item.get('document_number') or '').strip(), (item.get('html_url') or '').strip()
        if not key or key in ids or urlsplit(url).scheme != 'https':
            raise AuditStop('missing_or_repeated_eligible_identity')
        ids.append(key)
    return ids


def run(output, gate_dir):
    if os.environ.get('LIBERTREE_DB_BACKEND', 'sqlite') != 'sqlite':
        raise ValueError('pilot requires SQLite backend')
    output = Path(output); output.mkdir(parents=True, exist_ok=False)
    cls = CRAWLERS[SITE]; source = Path(cls.crawl.__code__.co_filename)
    report = {'started_at': utc_now(), 'site_id': SITE, 'state': 'running',
              'source_file_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
              'scope': 'first eleven code-eligible IDs from first native API window; actual SQLite saves',
              'requests': [], 'phases': [], 'full_collection_verified': False,
              'publisher_files_verified': False, 'operating_database_touched': False}
    def save():
        report['updated_at'] = utc_now(); atomic_json(output/'pilot.json', report)
    gate = RequestGate(gate_dir, delay=1.5)
    baseline = None; expected = None
    params = [('conditions[agencies][]', cls._AGENCY_SLUG), ('conditions[search_type_id]', '3'),
              ('order', 'newest'), ('per_page', str(cls._PAGE_SIZE)), ('page', '1')]
    def stop(signum, frame): raise AuditStop('wall_budget_reached')
    previous = signal.signal(signal.SIGALRM, stop); signal.alarm(1800); save()
    try:
        for number, (limit, cancel_at, required_new) in enumerate(((3, None, 3), (3, None, 3), (None, 2, 2), (3, None, 3)), 1):
            conn = sqlite3.connect(output/'pilot.sqlite')
            try:
                db_libertree.init_db(conn); db_libertree.upsert_site(conn, SITE, SITE, cls.base_url)
                count = lambda: conn.execute('SELECT count(*) FROM documents').fetchone()[0]
                before = count(); prior = dict(conn.execute('SELECT post_number,seq_id FROM documents'))
                phase = {'number': number, 'before': before, 'state': 'running', 'limit_new': limit, 'cancel_after': cancel_at}
                report['phases'].append(phase); save()
                crawler = cls(conn); crawler._session.close()
                crawler._session = AuditSession(gate, {urlsplit(cls._API_BASE).hostname}, report, save)
                original = crawler._session.request
                def request(method, url, **kwargs):
                    nonlocal baseline, expected
                    if (method.upper() != 'GET' or url != cls._API_BASE
                            or sorted(kwargs.get('params') or []) != sorted(params)):
                        raise AuditStop('request_outside_reviewed_first_window')
                    response = original(method, url, **kwargs)
                    if response.status_code != 200: raise AuditStop('source_http_failure')
                    data = response.json()
                    if not isinstance(data, dict) or not isinstance(data.get('results'), list):
                        raise AuditStop('source_schema_invalid')
                    if baseline is None:
                        baseline = data
                        candidates = eligible_ids(data['results'])
                        if len(candidates) < 11: raise AuditStop('first_window_has_fewer_than_eleven_eligible_ids')
                        expected = candidates[:11]
                        atomic_json(output/'first-api-window.json', data)
                        report.update(expected_ids=expected, source_reported_total=data.get('count'),
                                      source_reported_pages=data.get('total_pages'), first_window_records=len(data['results']),
                                      first_window_code_eligible=len(candidates), query_pairs=params,
                                      source_total_quality='reported_for_exact_native_query_not_whole_agency',
                                      source_pagination_cap=200, source_max_page_slots=200*cls._PAGE_SIZE)
                        save()
                    elif data['results'] != baseline['results'] or data.get('count') != baseline.get('count'):
                        raise AuditStop('source_changed_between_pilot_runs')
                    return response
                crawler._session.request = request; crawler.delivery_mode = 'incremental'
                if cancel_at: crawler.delivery_should_cancel = lambda: count() >= before+cancel_at
                try:
                    phase['returned_new'] = crawler.crawl(limit); phase['state'] = 'returned'
                except CrawlCancelled:
                    phase['state'] = 'cancelled'
                finally:
                    if phase['state'] == 'running': phase['state'] = 'interrupted'
                    phase.update(after=count(), actual_new=count()-before, source_failure=crawler.delivery_source_failure,
                                 crawler_exhausted=crawler.delivery_exhausted)
                    crawler._session.close(); save()
                after = dict(conn.execute('SELECT post_number,seq_id FROM documents'))
                if phase['actual_new'] != required_new or phase['source_failure'] or any(after.get(k) != v for k,v in prior.items()):
                    raise AuditStop('new_count_or_identity_preservation_failed')
                if cancel_at and phase['state'] != 'cancelled': raise AuditStop('cancel_not_observed')
                if not cancel_at and phase.get('returned_new') != required_new: raise AuditStop('returned_new_count_mismatch')
                if set(after) != set(expected[:count()]): raise AuditStop('native_saved_ids_differ_from_source_prefix')
            finally:
                conn.close()
        report.update(state='bounded_native_save_cancel_reopen_verified', stored_count=11,
                      missing_expected_ids=[], unexpected_ids=[])
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
    parser.add_argument('--output', required=True); parser.add_argument('--gate-dir', required=True)
    args = parser.parse_args(); result = run(args.output, args.gate_dir)
    print(json.dumps({'state': result['state'], 'stored_count': result.get('stored_count')}))
    return 0 if result['state'] == 'bounded_native_save_cancel_reopen_verified' else 1


if __name__ == '__main__':
    raise SystemExit(main())
