"""Original press catalogue with its official category filter, in a fresh owned DB."""
import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from crawler import db_libertree
from crawler.incremental import Collector, Policy, Store, init_schema
from crawler.incremental_audencia import AudenciaAdapter
from crawler.sites import CRAWLERS
from delivery.health.incremental_transport import install
from delivery.health.results import atomic_json, utc_now
from delivery.scripts.measure_incremental_batch import RequestBudgetReached


def request_kind(url):
    if urlsplit(url).path.lower().endswith('.pdf'): return 'pdf'
    if '/actualites/' in url: return 'detail'
    return 'list'


def run(output, gate_dir, *, max_requests=160):
    output = Path(output); output.mkdir(parents=True, exist_ok=False)
    site = 'audencia-com-lecole'; cls = CRAWLERS[site]
    report = dict(site_id=site, started_at=utc_now(), status='running', requests=[], phases=[],
                  scope={'category': '56', 'category_label': 'Formations', 'original_press_route': True},
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
                    collector = Collector(c, AudenciaAdapter(c, category='56'),
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
                assert not entry['requests_by_kind']['detail'] and not entry['requests_by_kind']['pdf']
                assert state['support'] == 'supported' and runs[-1]['stop_reason'] == 'known_boundary_plus_overlap'
                assert entry['http_requests'] < report['phases'][0]['http_requests']
                assert not c.delivery_exhausted
        report['status'] = 'passed_scoped_live_initial_resume_repeat'
    except BaseException as exc:
        report.update(status='failed_checkpoint_retained', error_type=type(exc).__name__, reason=str(exc)); raise
    finally:
        report['finished_at'] = utc_now(); report['durable_state'] = Store(conn, site).state()
        conn.close(); persist()
    return report


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--out', required=True); p.add_argument('--gate-dir', required=True)
    args = p.parse_args(); print(run(args.out, args.gate_dir)['status'])
