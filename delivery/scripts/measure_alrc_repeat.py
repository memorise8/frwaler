"""Live list-only repeat against a specifically owned synthetic-detail baseline."""
import argparse
from pathlib import Path
from urllib.parse import urlsplit

from crawler import db_pg
from crawler.incremental import Collector,Policy,Store
from crawler.incremental_alrc import ALRCAdapter
from crawler.alrc_scope import SCOPES,list_url
from crawler.sites import CRAWLERS
from delivery.health.incremental_transport import install
from delivery.health.results import atomic_json,utc_now


def run(dsn,base,out):
    if not urlsplit(dsn).path[1:].startswith('incremental_rollout_alrc_'):
        raise ValueError('requires owned ALRC measurement database')
    base,out=Path(base),Path(out);out.mkdir(exist_ok=False)
    conn=db_pg.open_db(dsn);c=CRAWLERS['alrc-gov-au-publications'](conn,delay=0)
    report=dict(status='running',started_at=utc_now(),requests=[],database=urlsplit(dsn).path[1:],
        baseline_contains_synthetic_detail_bodies=True,actual_detail_calls=0,
        global_source_complete=False,operating_database_touched=False,source_documents_deleted=False)
    def persist():atomic_json(out/'audit.json',report)
    before=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
    if len(before)!=927 or not Store(conn,c.site_id).state().get('baseline_complete'):
        c._session.close();conn.close()
        raise ValueError('owned baseline not complete for retained source-list fixture')
    install(c,base/'continuous-20261010/request-gate',out,report['requests'],persist=persist)
    gated=c._session.request
    def listing_only(method,url,**kwargs):
        if url not in {list_url(s,n) for s in SCOPES for n in range(1,101)}:raise ValueError('live detail request forbidden in fixture-baseline repeat')
        if len(report['requests'])>=20:raise ValueError('bounded repeat request budget reached')
        return gated(method,url,**kwargs)
    c._session.request=listing_only;c.delivery_mode='incremental';persist()
    try:
        collector=Collector(c,ALRCAdapter(c),Policy(full_check_days=0))
        saved=collector.run()
        after=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
        if saved or before!=after:raise ValueError('owned metadata baseline changed')
        if not 1<=len(report['requests'])<=20 or collector.stats['detail_requests']:
            raise ValueError('unexpected actual repeat request count')
        if collector.stats['stop_reason']!='known_boundary_plus_overlap':raise ValueError('unverified repeat stop')
        report.update(status='passed',stats=collector.stats,rows=len(after),existing_rows_unchanged=True,
                      actual_live_HTTP_requests=len(report['requests']),saved_new=0)
    except Exception as exc:
        report.update(status='failed_pending_retained',error_type=type(exc).__name__)
        raise
    finally:
        report['finished_at']=utc_now();persist();c._session.close();conn.close()
    print({'status':report['status'],'actual_live_HTTP_requests':len(report['requests']),'actual_detail_calls':0})


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--dsn',required=True);p.add_argument('--base',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();run(a.dsn,a.base,a.out)
