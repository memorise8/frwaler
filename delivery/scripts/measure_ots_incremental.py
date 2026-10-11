"""Bounded original-scope measurement in a new owned SQLite database."""
import argparse
import json
import os
from pathlib import Path

from crawler import db_libertree
from crawler.incremental import Collector, Policy, Store, init_schema
from crawler.incremental_ots import OTSAdapter
from crawler.sites import CRAWLERS
from delivery.health.incremental_transport import install
from delivery.health.request_gate import NetworkDeferred
from delivery.health.results import atomic_json, utc_now
from delivery.scripts.measure_incremental_batch import RequestBudgetReached


def run(output, gate_dir, *, query, max_requests=50, resume=False):
    output=Path(output)
    if resume:
        if not (output/'owned.sqlite').is_file():raise ValueError('owned checkpoint DB required')
    else:output.mkdir(parents=True,exist_ok=False)
    site='ots-at-pressemappe';cls=CRAWLERS[site]
    report={'site_id':site,'started_at':utc_now(),'status':'running','requests':[],'phases':[],
            'scope':{'query':query,'emittentId':'54','rows':3},
            'global_source_complete':False,'source_documents_deleted':False,
            'operating_database_touched':False,'request_gate':str(gate_dir)}
    if resume:
        previous=json.loads((output/'audit.json').read_text())
        if previous['scope']!=report['scope'] or previous['site_id']!=site or previous['status']!='failed':
            raise ValueError('resume requires matching failed measurement scope')
        report=previous
        report.setdefault('previous_failures',[]).append({k:report.get(k) for k in ('error_type','reason','finished_at')})
        report.pop('error_type',None);report.pop('reason',None)
        report.update(status='running',resumed_at=utc_now())
    def persist():atomic_json(output/'audit.json',report)
    persist()
    os.environ['LIBERTREE_DB_BACKEND']='sqlite'
    conn=db_libertree.open_db(output/'owned.sqlite');db_libertree.init_db(conn);init_schema(conn)
    db_libertree.upsert_site(conn,site,cls.site_name,cls.base_url)
    class SmallOTS(OTSAdapter):rows=3
    try:
        phases=[('initial_partial_delivery',3),('resume_delivery',None),('repeat',None)]
        if resume:
            recorded=sum(p['http_requests'] for p in report['phases']);unrecorded=report['requests'][recorded:]
            rows=conn.execute('SELECT count(*) FROM documents').fetchone()[0]
            if unrecorded:
                report['phases'].append({'phase':'interrupted_resume','http_requests':len(unrecorded),
                    'detail_http_requests':sum('/pressrelease/' in r['url'] for r in unrecorded),
                    'saved_new':rows-report['phases'][-1]['rows_after'],'rows_after':rows,
                    'interrupted':True,'not_a_completed_phase':True})
            phases=[('resume_delivery_after_fix',None),('repeat',None)]
        for phase,limit in phases:
            c=cls(conn,delay=0)
            install(c,gate_dir,output,report['requests'],persist=persist)
            transport=c._session.request
            def budgeted(method,url,**kwargs):
                if len(report['requests'])>=max_requests:raise RequestBudgetReached()
                return transport(method,url,**kwargs)
            c._session.request=budgeted
            before=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
            collector=Collector(c,SmallOTS(c,query=query),
                                Policy(max_pages=20,wall_seconds=240,full_check_days=0))
            start=len(report['requests']);saved=collector.run(limit=limit)
            after=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
            state=Store(conn,site).state();requests=report['requests'][start:]
            entry={'phase':phase,'http_requests':len(requests),'engine':collector.stats,
                   'detail_http_requests':sum('/pressrelease/' in r['url'] for r in requests),
                   'saved_new':saved,'rows_before':len(before),'rows_after':len(after),
                   'existing_rows_unchanged':after[:len(before)]==before,'state':state}
            assert entry['existing_rows_unchanged']
            report['phases'].append(entry);persist()
            print(json.dumps({k:entry[k] for k in ('phase','http_requests','detail_http_requests','saved_new','rows_after')}),flush=True)
            if phase=='initial_partial_delivery':
                assert not state.get('baseline_complete',False)
                assert state['discovery_exhausted']
            elif state['phase']!='idle':
                report['status']='checkpointed_incomplete';break
            if phase.startswith('resume_delivery'):
                assert entry['http_requests']==entry['detail_http_requests']
                assert state['baseline_complete'] and state['order']=='newest_first'
            if phase=='repeat':
                assert saved==0 and collector.stats['save_attempts']==0
                assert entry['detail_http_requests']==0
                assert state['support']=='supported'
                assert collector.stats['stop_reason']=='known_boundary_plus_overlap'
                assert entry['http_requests']<report['phases'][0]['http_requests']
                assert not c.delivery_exhausted
        else:report['status']='passed_bounded_live_initial_resume_repeat'
    except NetworkDeferred as exc:report.update(status='network_deferred',retry_at=exc.retry_at)
    except RequestBudgetReached:report.update(status='request_budget_checkpoint')
    except Exception as exc:
        report.update(status='failed',error_type=type(exc).__name__,reason=str(exc));raise
    finally:
        report['finished_at']=utc_now();report['durable_state']=Store(conn,site).state()
        conn.close();persist()
    return report


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);p.add_argument('--gate-dir',required=True);p.add_argument('--query',required=True);p.add_argument('--resume',action='store_true')
    args=p.parse_args();result=run(args.out,args.gate_dir,query=args.query,resume=args.resume)
    print(result['status']);return 0 if result['status'].startswith('passed') else 1

if __name__=='__main__':raise SystemExit(main())
