"""Replay hash-verified source observations in NEW owned SQLite databases.

This is offline regression evidence, not fresh HTTP or a global source census.
No original database is opened; only recorded response bodies are read.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import requests

from crawler import db_libertree
from crawler.incremental import Collector, Policy, Store, init_schema
from crawler.incremental_adapters import DOAJAdapter, HALAdapter
from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json, utc_now


class RecordedTransport:
    def __init__(self, root, records):
        self.records = records
        self.bodies = []
        self.calls = []
        # Validate every response before starting any collection phase.
        for i, record in enumerate(records):
            body = (Path(root) / f'{i:03d}.body').read_bytes()
            if (len(body) != record['bytes'] or
                    hashlib.sha256(body).hexdigest() != record['sha256']):
                raise ValueError(f'recorded response integrity failed: {i}')
            self.bodies.append(body)

    def __call__(self, method, url, **kwargs):
        i = len(self.calls)
        if i >= len(self.records):
            raise AssertionError('unrecorded request; network disabled')
        record = self.records[i]
        prepared = requests.Request(method, url, params=kwargs.get('params')).prepare()
        if prepared.method != record['method'] or prepared.url != record['url']:
            raise AssertionError(f'request contract differs from observation {i}')
        response = requests.Response()
        response.status_code = record['status']
        response.url = prepared.url
        response.request = prepared
        response._content = self.bodies[i]
        response.headers['Content-Type'] = 'application/json'
        self.calls.append({'method': prepared.method, 'url': prepared.url,
                           'recorded_response_sha256': record['sha256']})
        return response


def rows(conn):
    return [dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]


def run(source, output):
    source, output = Path(source), Path(output)
    observations = json.loads((source / 'results.json').read_text())
    output.mkdir(parents=True, exist_ok=False)
    report = {'kind': 'offline_replay_of_retained_live_responses', 'started_at': utc_now(),
              'input': str(source), 'input_sha256': hashlib.sha256((source/'results.json').read_bytes()).hexdigest(),
              'status': 'running', 'new_live_http_requests': 0, 'sites': [],
              'production_database_changed': False, 'global_source_complete': False}
    def persist():
        atomic_json(output / 'audit.json', report)
    persist()
    try:
        # Even an accidental unpatched session cannot open a socket.
        with patch.dict(os.environ, {'LIBERTREE_DB_BACKEND': 'sqlite'}), \
                patch('requests.sessions.Session.request', side_effect=AssertionError('network disabled')):
            for record in observations['sites']:
                site = record['site_id']
                if site not in ('doaj-org-search', 'cea-hal-science-cnrgh'):
                    continue
                transport = RecordedTransport(source / site, record['requests'])
                directory = output / site
                directory.mkdir()
                conn = db_libertree.open_db(directory / 'owned.sqlite')
                result = {'site_id': site, 'phases': [], 'requests': transport.calls,
                          'historical_live_http_requests': len(record['requests'])}
                report['sites'].append(result)
                try:
                    db_libertree.init_db(conn); init_schema(conn)
                    db_libertree.upsert_site(conn, site, site, CRAWLERS[site].base_url)
                    original = {p['phase']: p for p in record['phases']}
                    scope = original['initial']['scope']
                    result['scope'] = scope
                    for phase, limit in [('initial_partial', 3), ('resume', None),
                                         ('repeat', None), ('remove_latest3_repeat', None)]:
                        removed = []
                        if phase == 'remove_latest3_repeat':
                            removed = [r[0] for r in conn.execute(
                                'SELECT post_number FROM documents ORDER BY seq_id DESC LIMIT 3')]
                            assert set(removed) == set(original[phase]['removed'])
                            conn.executemany('DELETE FROM documents WHERE post_number=?', [(x,) for x in removed])
                            conn.commit()  # only this newly-created owned SQLite DB
                        before = rows(conn)
                        crawler = CRAWLERS[site](conn, delay=0)
                        crawler._session.request = transport
                        adapter = (DOAJAdapter(crawler, scope['query']) if site == 'doaj-org-search'
                                   else HALAdapter(crawler, scope))
                        if isinstance(adapter, DOAJAdapter): adapter.size = 3
                        start = len(transport.calls)
                        collector = Collector(crawler, adapter, Policy(max_pages=20, wall_seconds=120, full_check_days=0))
                        saved = collector.run(limit=limit)
                        after = rows(conn)
                        state = Store(conn, site).state()
                        observed = {'phase': phase, 'replayed_request_calls': len(transport.calls)-start,
                                    'detail_request_calls': collector.stats['detail_requests'],
                                    'saved_new': saved, 'removed_in_owned_db': removed,
                                    'rows_before': len(before), 'rows_after': len(after),
                                    'existing_rows_unchanged': after[:len(before)] == before,
                                    'engine': collector.stats, 'state': state}
                        result['phases'].append(observed); persist()
                        assert observed['existing_rows_unchanged']
                        assert observed['detail_request_calls'] == 0
                        if phase == 'initial_partial':
                            assert saved == 3 and not state.get('baseline_complete')
                            assert state['phase'] == 'deliver' and state['discovery_exhausted']
                            assert observed['replayed_request_calls'] == original['initial']['http_requests']
                            # Close the DB: continuation must not depend on Python objects.
                            conn.close()
                            conn = db_libertree.open_db(directory / 'owned.sqlite')
                        else:
                            assert state['phase'] == 'idle' and state['baseline_complete']
                            assert state['support'] == 'supported'
                            assert len(after) == original['initial']['rows_after']
                            if phase == 'resume':
                                assert observed['replayed_request_calls'] == 0
                                dates = [r['listed_date'] for r in after]
                                assert dates == sorted(dates)
                            else:
                                assert saved == len(removed)
                                assert observed['replayed_request_calls'] == original[phase]['http_requests']
                                assert collector.stats['save_attempts'] == len(removed)
                                assert collector.stats['stop_reason'] == 'known_boundary_plus_overlap'
                        print(site, phase, observed['replayed_request_calls'], saved, flush=True)
                    assert len(transport.calls) == len(record['requests'])
                    result['status'] = 'passed'
                finally:
                    conn.close(); persist()
            assert {x['site_id'] for x in report['sites']} == {'doaj-org-search', 'cea-hal-science-cnrgh'}
        report['status'] = 'passed'
    except BaseException as exc:
        report.update(status='failed', error_type=type(exc).__name__, reason=str(exc))
        raise
    finally:
        report['finished_at'] = utc_now(); persist()
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    run(args.source, args.out)


if __name__ == '__main__':
    main()
