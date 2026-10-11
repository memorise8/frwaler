"""A bounded rollout measurement in a fresh owned DB; never deletes documents."""
import argparse
import hashlib
import json
import os
from pathlib import Path

from crawler import db_libertree
from crawler.incremental import Collector, Policy, Store, init_schema
from crawler.incremental_federal import FederalAdapter
from crawler.sites import CRAWLERS
from delivery.health.incremental_transport import install
from delivery.health.request_gate import NetworkDeferred
from delivery.health.results import atomic_json, utc_now


class RequestBudgetReached(BaseException):
    pass


def run(output, gate_dir, *, lower, upper, rows=10, max_requests=40):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    site='federalregister-gov-documents';cls=CRAWLERS[site]
    report={'site_id':site,'started_at':utc_now(),'status':'running','requests':[],'phases':[],
            'scope':{'lower':lower,'upper':upper,'agency':cls._AGENCY_SLUG,'rows':rows},
            'global_source_complete':False,'source_documents_deleted':False,
            'operating_database_touched':False,'request_gate':str(gate_dir)}
    def persist():atomic_json(output/'audit.json',report)
    persist()
    os.environ['LIBERTREE_DB_BACKEND']='sqlite'
    conn=db_libertree.open_db(output/'owned.sqlite');db_libertree.init_db(conn);init_schema(conn)
    db_libertree.upsert_site(conn,site,cls.site_name,cls.base_url)
    try:
        for phase,limit in [('initial_partial_delivery',3),('resume_delivery',None),('repeat',None)]:
            c=cls(conn,delay=0);c._PAGE_SIZE=rows
            install(c,gate_dir,output,report['requests'],persist=persist)
            transport=c._session.request
            def budgeted(method,url,**kwargs):
                if len(report['requests'])>=max_requests:raise RequestBudgetReached()
                return transport(method,url,**kwargs)
            c._session.request=budgeted
            before=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
            collector=Collector(c,FederalAdapter(c,lower=lower,upper=upper),Policy(max_pages=30,wall_seconds=180,full_check_days=0))
            start=len(report['requests']);saved=collector.run(limit=limit)
            after=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
            state=Store(conn,site).state()
            entry={'phase':phase,'http_requests':len(report['requests'])-start,'engine':collector.stats,
                   'saved_new':saved,'rows_before':len(before),'rows_after':len(after),
                   'existing_rows_unchanged':after[:len(before)]==before,'state':state}
            assert entry['existing_rows_unchanged']
            report['phases'].append(entry);persist()
            print(json.dumps({k:entry[k] for k in ('phase','http_requests','saved_new','rows_after')}),flush=True)
            if phase=='initial_partial_delivery' and state.get('baseline_complete'):
                raise AssertionError('sample too small to exercise durable partial delivery')
            if phase!='initial_partial_delivery' and state['phase']!='idle':
                report['status']='checkpointed_incomplete';break
            if phase=='repeat':
                assert saved==0 and collector.stats['save_attempts']==0
                assert state['support']=='supported'
                assert collector.stats['stop_reason']=='known_boundary_plus_overlap'
                assert entry['http_requests']<report['phases'][0]['http_requests']
                assert not c.delivery_exhausted
        else:report['status']='passed_bounded_live_initial_resume_repeat'
    except NetworkDeferred as exc:
        report.update(status='network_deferred',retry_at=exc.retry_at)
    except RequestBudgetReached:
        report.update(status='request_budget_checkpoint')
    except Exception as exc:
        report.update(status='failed',error_type=type(exc).__name__,reason=str(exc))
        raise
    finally:
        report['finished_at']=utc_now()
        report['durable_state']=Store(conn,site).state()
        conn.close();persist()
    return report


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);p.add_argument('--gate-dir',required=True)
    p.add_argument('--lower',required=True);p.add_argument('--upper',required=True);p.add_argument('--rows',type=int,default=10)
    args=p.parse_args();result=run(args.out,args.gate_dir,lower=args.lower,upper=args.upper,rows=args.rows)
    print(result['status']);return 0 if result['status'].startswith('passed') else 1

if __name__=='__main__':raise SystemExit(main())
