"""Measure a bounded incremental repeat against an owned 223-row fixture."""
import argparse
import os
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from crawler import db_pg
from crawler.incremental import Collector, Policy, Store
from crawler.sites import CRAWLERS
from delivery.incremental_registry import adapter_for
from delivery.health.incremental_transport import install
from delivery.health.results import atomic_json, utc_now
from delivery.scripts.review_artsetmetiers_incremental import SITE, retained


def run(dsn, base, out):
    if not urlsplit(dsn).path[1:].startswith('incremental_rollout_artsetmetiers_'):
        raise ValueError('owned Arts fixture database required')
    base, out = Path(base), Path(out); out.mkdir(parents=True, exist_ok=False)
    pages, items, papers, _ = retained(base/'continuous-20261010')
    conn = db_pg.open_db(dsn); c = CRAWLERS[SITE](conn, delay=0); a = adapter_for(c)
    before = [dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
    state = Store(conn, SITE).state()
    if (len(before) != 223 or {r['meta_url'] for r in before} != {p['url'] for p in papers}
            or not state['baseline_complete'] or state['support'] != 'supported' or state['phase'] != 'idle'):
        raise ValueError('owned Arts223 completed fixture required')
    requests = []; report = dict(status='running', started_at=utc_now(), requests=requests,
                               fixture_database=True, operating_database_touched=False)
    def persist(): atomic_json(out/'audit.json', report)
    install(c, base/'continuous-20261010/request-gate', out, requests, persist=persist)
    audited = c._session.request; sequence = [0,0,1]
    def bounded(method, url, **kwargs):
        if len(requests) >= 3 or url != a.url(sequence[len(requests)]):
            raise ValueError('Arts bounded repeat exceeded expected list scope')
        n = sequence[len(requests)]; r = audited(method, url, **kwargs)
        if a.inspect(r.content,n) != a.inspect(pages[n],n): raise ValueError('Arts current listing changed')
        return r
    c._session.request = bounded; persist()
    try:
        with patch.dict(os.environ, {'LIBERTREE_DB_BACKEND':'postgres'}):
            col = Collector(c,a,Policy(full_check_days=0)); saved = col.run()
        after = [dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
        if (saved or before != after or len(requests) != 3 or col.stats['detail_requests']
                or col.stats['stop_reason'] != 'known_boundary_plus_overlap'):
            raise ValueError('Arts incremental repeat failed invariants')
        report.update(status='passed', actual_HTTP_requests=3, actual_detail_requests=0, actual_PDF_requests=0,
                      saved_new=0, save_attempts=col.stats['save_attempts'], rows=223, existing_rows_unchanged=True,
                      stop_condition=col.stats['stop_reason'], known_item_early_stop=True,
                      fixture_baseline_complete=True, global_source_complete=False, all_files_verified=False,
                      finished_at=utc_now())
    except Exception as exc:
        report.update(status='failed', error_type=type(exc).__name__, finished_at=utc_now()); raise
    finally: persist(); c._session.close(); conn.close()
    print({k:report[k] for k in ('status','actual_HTTP_requests','actual_detail_requests','saved_new')})


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--dsn',required=True);p.add_argument('--base',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();run(a.dsn,a.base,a.out)
