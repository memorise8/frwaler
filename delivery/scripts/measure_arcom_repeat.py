"""Bounded live listing-only repeat against an explicitly owned fixture database."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from crawler import db_pg
from crawler.incremental import Collector,Policy,Store
from crawler.incremental_arcom import ARCOMAdapter
from crawler.sites import CRAWLERS
from delivery.health.incremental_transport import install
from delivery.health.results import atomic_json,utc_now

SITE='arcom-fr-presse'


def run(dsn,base,out):
    if not urlsplit(dsn).path[1:].startswith('incremental_rollout_arcom_'):raise ValueError('owned ARCOM database required')
    base=Path(base);out=Path(out);out.mkdir(parents=True,exist_ok=False)
    conn=db_pg.open_db(dsn);c=CRAWLERS[SITE](conn,delay=0);a=ARCOMAdapter(c);requests=[]
    before=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
    state=Store(conn,SITE).state()
    if len(before)!=310 or not state.get('baseline_complete') or state.get('phase')!='idle' or state.get('contract')!=a.contract:
        raise ValueError('complete 310-row fixture baseline required')
    report=dict(status='running',started_at=utc_now(),fixture_database=True,fixture_baseline_contains_synthetic_details=True,
        actual_prior_full_detail_collection=False,requests=requests,operating_database_touched=False)
    def persist():atomic_json(out/'audit.json',report)
    c._session.headers['Accept-Encoding']='gzip, deflate'
    install(c,base/'continuous-20261010/request-gate',out,requests,persist=persist);native=c._session.request
    allowed={a.url(i) for i in range(4)}
    def guarded(method,url,**kwargs):
        if method.upper()!='GET' or url not in allowed or len(requests)>=8:raise ValueError('listing-only live request budget/scope exceeded')
        return native(method,url,**kwargs)
    c._session.request=guarded;persist()
    try:
        col=Collector(c,a,Policy(full_check_days=0));saved=col.run()
        after=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
        if saved or col.stats['detail_requests'] or before!=after or col.stats['stop_reason']!='known_boundary_plus_overlap':
            raise ValueError('ARCOM live repeat did not preserve fixture rows or expected boundary')
        report.update(status='passed',saved_new=0,rows=310,actual_HTTP_requests=len(requests),actual_detail_requests=0,
            stop_reason=col.stats['stop_reason'],existing_rows_unchanged=True,global_source_complete=False,
            all_files_verified=False,finished_at=utc_now())
    except Exception as exc:
        report.update(status='failed',error_type=type(exc).__name__,finished_at=utc_now());raise
    finally:persist();c._session.close();conn.close()
    print({k:report[k] for k in ('status','actual_HTTP_requests','actual_detail_requests','saved_new')})


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--dsn',required=True);p.add_argument('--base',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();run(a.dsn,a.base,a.out)
