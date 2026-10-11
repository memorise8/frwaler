"""Replay recent actual transport with the final guard, then measure live repeat."""
import argparse
import json
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

from crawler import db_libertree
from crawler.incremental import Collector,Policy,Store,init_schema
from crawler.incremental_ens_lyon import EnsLyonAdapter
from crawler.sites import CRAWLERS
from delivery.health.incremental_transport import install
from delivery.health.results import atomic_json,utc_now


def run(capture,out,gate_dir):
    capture,out=Path(capture),Path(out);out.mkdir(parents=True,exist_ok=False)
    audit=json.loads((capture/'audit.json').read_text());assert audit['status']=='passed_full_metadata_scope_live_initial_resume_repeat'
    responses={}
    for r in audit['requests']:
        raw=(capture/r['body']).read_bytes();assert hashlib.sha256(raw).hexdigest()==r['sha256'] and r['status']==200
        responses[r['url']]=raw.decode('utf-8')
    os.environ['LIBERTREE_DB_BACKEND']='sqlite';conn=db_libertree.open_db(out/'owned.sqlite');db_libertree.init_db(conn);init_schema(conn)
    cls=CRAWLERS['ens-lyon-fr-lecole'];db_libertree.upsert_site(conn,cls.site_id,cls.site_name,cls.base_url)
    calls=[];phases=[]
    try:
        for phase,limit in [('partial',3),('resume',None),('repeat',None)]:
            c=cls(conn,delay=0);start=len(calls)
            def request(url,**kw):calls.append(url);return SimpleNamespace(text=responses[url])
            c._request=request;collector=Collector(c,EnsLyonAdapter(c),Policy(full_check_days=0));saved=collector.run(limit)
            state=Store(conn,c.site_id).state();c._session.close()
            phases.append(dict(phase=phase,saved_new=saved,requests=len(calls)-start,details=sum('?page=' not in u for u in calls[start:]),state=state))
        assert [p['saved_new'] for p in phases]==[3,235,0]
        assert [p['requests'] for p in phases]==[279,1,3]
        assert calls==[r['url'] for r in audit['requests']]
        before=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
        import sqlite3
        original=sqlite3.connect('file:'+str(capture/'owned.sqlite')+'?mode=ro',uri=True);original.row_factory=sqlite3.Row
        original_rows=[dict(r) for r in original.execute('SELECT * FROM documents ORDER BY seq_id')];original.close()
        clean=lambda rows:[{k:v for k,v in row.items() if k!='collected_at'} for row in rows]
        assert clean(before)==clean(original_rows)
        replay=dict(status='passed',reviewed_at=utc_now(),fixture_transport=True,live_http_requests=0,
                    captured_transport_replayed=True,actual_request_sequence_identical=True,
                    all_stored_fields_identical_except_collection_timestamp=True,phases=phases,
                    stored_eligible_records=len(before),source_records=239,native_exclusions=['1605424'],
                    ambiguous_duplicate_memberships=0,operating_database_touched=False,source_documents_deleted=False)
        atomic_json(out/'replay-review.json',replay)
        live_dir=out/'live-repeat';live_dir.mkdir(exist_ok=False)
        live=dict(status='running',started_at=utc_now(),requests=[],baseline_origin='current_code_replay_of_recent_actual_source_responses',operating_database_touched=False,source_documents_deleted=False)
        def persist():atomic_json(live_dir/'audit.json',live)
        c=cls(conn,delay=0);install(c,gate_dir,live_dir,live['requests'],persist=persist)
        try:
            collector=Collector(c,EnsLyonAdapter(c),Policy(full_check_days=0,max_pages=5,wall_seconds=90))
            saved=collector.run();after=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
            state=Store(conn,c.site_id).state()
            assert saved==0 and after==before and len(live['requests'])==3
            assert collector.stats['detail_requests']==0 and collector.stats['stop_reason']=='known_boundary_plus_overlap'
            live.update(status='passed',saved_new=saved,engine=collector.stats,existing_rows_unchanged=True,
                        rows=len(after),state=state,full_files_verified=False)
        except BaseException as exc:
            live.update(status='failed_checkpoint_retained',error_type=type(exc).__name__,reason=str(exc));raise
        finally:c._session.close();persist()
        print(json.dumps({'replay':'passed','live_repeat':live['status'],'actual_repeat_requests':len(live['requests']),'detail_requests':collector.stats['detail_requests']}))
    finally:conn.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--capture',required=True);p.add_argument('--out',required=True);p.add_argument('--gate-dir',required=True)
    args=p.parse_args();run(args.capture,args.out,args.gate_dir)
