"""Both original ASN report catalogues, in a fresh owned DB."""
import argparse
import json
import os
from pathlib import Path
import sqlite3

from crawler import db_libertree
from crawler.incremental import Collector, Policy, Store, init_schema
from crawler.incremental_asn import ASNAdapter
from crawler.sites import CRAWLERS
from delivery.health.incremental_transport import install
from delivery.health.results import atomic_json, utc_now
from delivery.scripts.measure_incremental_batch import RequestBudgetReached


def request_kind(url):
    return 'detail' if '/rapports-d-expertise/' in url or '/rapports-de-l-asnr/' in url else 'list'


def verify_recorded_phases(report):
    """Verify captured runs, including explicit native-exclusion rechecks."""
    phases = report['phases']
    assert [p['phase'] for p in phases] == ['initial_partial_delivery', 'resume_delivery', 'repeat']
    first, resume, repeat = phases
    assert first['saved_new'] == 3 and not first['state'].get('baseline_complete', False)
    assert first['state']['discovery_exhausted'] and first['state']['phase'] == 'deliver'
    assert resume['state']['baseline_complete'] and resume['state']['order'] == 'newest_first'
    assert resume['state']['phase'] == 'idle' and resume['requests_by_kind']['list'] == 0
    assert repeat['state']['phase'] == 'idle' and repeat['state']['scan'] == 'incremental'
    assert repeat['state']['support'] == 'supported' and repeat['state']['baseline_complete']
    assert repeat['saved_new'] == 0 and sum(r['save_attempts'] for r in repeat['engine_runs']) == 0
    assert repeat['engine_runs'][-1]['stop_reason'] in ('known_boundary_plus_overlap', 'source_exhausted')
    assert repeat['http_requests'] < first['http_requests']
    offset = 0
    rechecks = {}
    for phase in phases:
        reqs = report['requests'][offset:offset+phase['http_requests']]; offset += len(reqs)
        assert len(reqs) == phase['http_requests'] and phase['existing_rows_unchanged']
        kinds = {k: sum(request_kind(r['url']) == k for r in reqs) for k in ('list', 'detail', 'pdf')}
        assert kinds == phase['requests_by_kind'] and not kinds['pdf']
        if phase['phase'] != 'initial_partial_delivery':
            urls = [r['url'] for r in reqs if request_kind(r['url']) == 'detail']
            exclusions = phase['state']['cursor']['exclusions']
            assert set(urls) <= set(exclusions) and len(urls) == len(set(urls))
            rechecks[phase['phase']] = urls
    assert offset == len(report['requests'])
    assert report['durable_state']['phase'] == 'idle' and report['durable_state']['baseline_complete']
    return rechecks


def run(output, gate_dir, *, max_requests=160):
    output = Path(output); output.mkdir(parents=True, exist_ok=False)
    site = 'asn-fr-information'; cls = CRAWLERS[site]
    report = dict(site_id=site, started_at=utc_now(), status='running', requests=[], phases=[],
                  scope={'original_delivered_catalogues': ['expertise', 'annual'], 'PDF_bodies_verified': False},
                  global_source_complete=False, source_documents_deleted=False,
                  operating_database_touched=False, request_gate=str(gate_dir))
    def persist(): atomic_json(output / 'audit.json', report)
    persist(); os.environ['LIBERTREE_DB_BACKEND'] = 'sqlite'
    conn = db_libertree.open_db(output / 'owned.sqlite'); db_libertree.init_db(conn); init_schema(conn)
    db_libertree.upsert_site(conn, site, cls.site_name, cls.base_url)
    try:
        for phase, limit in [('initial_partial_delivery', 3), ('resume_delivery', None), ('repeat', None)]:
            start = len(report['requests']); saved_total = 0; runs = []
            before = [dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
            for attempt in range(5):
                c = cls(conn, delay=0)
                install(c, gate_dir, output, report['requests'], persist=persist)
                transport = c._session.request
                def budgeted(method, url, **kwargs):
                    if len(report['requests']) >= max_requests: raise RequestBudgetReached()
                    return transport(method, url, **kwargs)
                c._session.request = budgeted
                try:
                    collector = Collector(c, ASNAdapter(c),
                                          Policy(max_pages=100, wall_seconds=180, full_check_days=0))
                    saved_total += collector.run(limit=None if limit is None else limit-saved_total)
                    runs.append(collector.stats)
                    state = Store(conn, site).state()
                    if state['phase'] == 'idle' or (limit is not None and saved_total >= limit): break
                finally: c._session.close()
            else: raise ValueError('bounded run budget exhausted; checkpoint retained')
            after = [dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
            requests = report['requests'][start:]
            entry = dict(phase=phase, http_requests=len(requests), saved_new=saved_total,
                         rows_before=len(before), rows_after=len(after), engine_runs=runs, state=state,
                         existing_rows_unchanged=after[:len(before)] == before,
                         requests_by_kind={k: sum(request_kind(r['url']) == k for r in requests)
                                           for k in ('list', 'detail', 'pdf')})
            assert entry['existing_rows_unchanged']
            report['phases'].append(entry); persist()
            print(json.dumps({k:entry[k] for k in ('phase','http_requests','saved_new','rows_after','requests_by_kind')}), flush=True)
            if phase == 'initial_partial_delivery':
                assert saved_total == 3 and not state.get('baseline_complete', False) and state['discovery_exhausted']
            elif phase == 'resume_delivery':
                assert state['baseline_complete'] and state['order'] == 'newest_first'
                assert not entry['requests_by_kind']['list']
            else:
                assert saved_total == 0 and sum(r['save_attempts'] for r in runs) == 0
                # Native exclusions are rechecked. These are not repeated
                # detail lookups for the already stored documents.
                detail_urls = [r['url'] for r in requests if request_kind(r['url']) == 'detail']
                assert set(detail_urls) <= set(state['cursor']['exclusions'])
                assert len(detail_urls) == len(set(detail_urls)) and not entry['requests_by_kind']['pdf']
                assert state['support'] == 'supported' and runs[-1]['stop_reason'] in ('known_boundary_plus_overlap', 'source_exhausted')
                assert entry['http_requests'] < report['phases'][0]['http_requests']
                assert not c.delivery_exhausted
        report['status'] = 'passed_scoped_live_initial_resume_repeat'
    except BaseException as exc:
        report.update(status='failed_checkpoint_retained', error_type=type(exc).__name__, reason=str(exc)); raise
    finally:
        report['finished_at'] = utc_now(); report['durable_state'] = Store(conn, site).state()
        conn.close(); persist()
    return report


def repeat_owned_baseline(source_root, output, gate_dir):
    """Read-only copy of an independently verified owned DB; never production."""
    source_root, output = Path(source_root), Path(output)
    original = json.loads((source_root/'audit.json').read_text())
    verify_recorded_phases(original)
    assert original['operating_database_touched'] is False
    output.mkdir(parents=True, exist_ok=False)
    report = dict(site_id=original['site_id'], status='running', started_at=utc_now(), requests=[],
                  source_owned_database=str(source_root/'owned.sqlite'), operating_database_touched=False,
                  source_database_opened_read_only=True, source_documents_deleted=False)
    def persist(): atomic_json(output/'audit.json', report)
    persist(); os.environ['LIBERTREE_DB_BACKEND'] = 'sqlite'
    source = sqlite3.connect('file:'+str(source_root/'owned.sqlite')+'?mode=ro', uri=True)
    conn = db_libertree.open_db(output/'owned.sqlite')
    try:
        source.backup(conn)
    finally: source.close()
    cls = CRAWLERS[report['site_id']]; c = cls(conn, delay=0)
    try:
        adapter = ASNAdapter(c); before_state = Store(conn, c.site_id).state()
        assert before_state['baseline_complete'] and before_state['contract'] == adapter.contract
        before = [dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
        install(c, gate_dir, output, report['requests'], persist=persist)
        collector = Collector(c, adapter, Policy(full_check_days=0, max_pages=100, wall_seconds=300))
        saved = collector.run()
        after = [dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
        state = Store(conn, c.site_id).state()
        details = [r['url'] for r in report['requests'] if request_kind(r['url']) == 'detail']
        assert saved == collector.stats['save_attempts'] == 0 and before == after
        assert state['phase'] == 'idle' and state['baseline_complete'] and state['support'] == 'supported'
        assert state['scan'] == 'incremental' and set(details) <= set(state['cursor']['exclusions'])
        assert len(details) == len(set(details))
        report.update(status='passed_actual_repeat_on_owned_baseline_copy', saved_new=saved,
            engine=collector.stats, state=state, rows=len(after), existing_rows_unchanged=True,
            exclusion_detail_rechecks=details, http_requests=len(report['requests']),
            requests_by_kind={k:sum(request_kind(r['url']) == k for r in report['requests']) for k in ('list','detail','pdf')})
    except BaseException as exc:
        report.update(status='failed_checkpoint_retained', error_type=type(exc).__name__, reason=str(exc)); raise
    finally:
        report['finished_at']=utc_now(); persist(); c._session.close(); conn.close()
    return report


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--out', required=True); p.add_argument('--gate-dir', required=True)
    p.add_argument('--repeat-owned-baseline')
    args = p.parse_args()
    result = repeat_owned_baseline(args.repeat_owned_baseline,args.out,args.gate_dir) if args.repeat_owned_baseline else run(args.out,args.gate_dir)
    print(result['status'])
