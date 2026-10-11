"""Bounded original-scope measurement in a new owned SQLite database."""
import argparse
import json
import os
from pathlib import Path

from crawler import db_libertree
from crawler.incremental import Collector, Policy, Store, init_schema
from crawler.incremental_acpr import ACPRAdapter
from crawler.sites import CRAWLERS
from delivery.health.incremental_transport import install
from delivery.health.request_gate import NetworkDeferred
from delivery.health.results import atomic_json, utc_now
from delivery.scripts.measure_incremental_batch import RequestBudgetReached


def run(output, gate_dir, *, first_year, last_year, max_requests=85):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    site='acpr-banque-france-fr-fr';cls=CRAWLERS[site]
    report={'site_id':site,'started_at':utc_now(),'status':'running','requests':[],'phases':[],
            'scope':{'scopes':['studies'],'years':[first_year,last_year],'rows':12},
            'global_source_complete':False,'source_documents_deleted':False,
            'operating_database_touched':False,'request_gate':str(gate_dir)}
    def persist():atomic_json(output/'audit.json',report)
    persist()
    os.environ['LIBERTREE_DB_BACKEND']='sqlite'
    conn=db_libertree.open_db(output/'owned.sqlite');db_libertree.init_db(conn);init_schema(conn)
    db_libertree.upsert_site(conn,site,cls.site_name,cls.base_url)
    try:
        for phase,limit in [('initial_partial_delivery',3),('resume_delivery',None),('repeat',None)]:
            c=cls(conn,delay=0)
            install(c,gate_dir,output,report['requests'],persist=persist)
            transport=c._session.request
            def budgeted(method,url,**kwargs):
                if len(report['requests'])>=max_requests:raise RequestBudgetReached()
                return transport(method,url,**kwargs)
            c._session.request=budgeted
            before=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
            collector=Collector(c,ACPRAdapter(c,scopes=['studies'],years=(first_year,last_year)),
                                Policy(max_pages=20,wall_seconds=240,full_check_days=0))
            start=len(report['requests']);saved=collector.run(limit=limit)
            after=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
            state=Store(conn,site).state();requests=report['requests'][start:]
            entry={'phase':phase,'http_requests':len(requests),'engine':collector.stats,
                   'detail_http_requests':sum('/publications/' in r['url'] for r in requests),
                   'pdf_http_requests':sum('/system/files/' in r['url'] for r in requests),
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
            if phase=='resume_delivery':
                assert entry['http_requests']==entry['detail_http_requests']+entry['pdf_http_requests']
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
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);p.add_argument('--gate-dir',required=True);p.add_argument('--first-year',type=int,required=True);p.add_argument('--last-year',type=int,required=True)
    args=p.parse_args();result=run(args.out,args.gate_dir,first_year=args.first_year,last_year=args.last_year)
    print(result['status']);return 0 if result['status'].startswith('passed') else 1

if __name__=='__main__':raise SystemExit(main())
