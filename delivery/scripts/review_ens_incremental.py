"""Replay all retained ENS source memberships and actual detail bodies in a new DB."""
import argparse
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

from crawler import db_libertree
from crawler.incremental import Collector,Policy,Store,init_schema
from crawler.incremental_ens_lyon import EnsLyonAdapter
from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json,utc_now


def run(source,out):
    source,out=Path(source),Path(out);out.mkdir(parents=True,exist_ok=False)
    prior=json.loads((source/'ens-lyon-press-enumeration-independent-review.json').read_text())
    path=source/'ens-lyon-press-enumeration-followup/audit.json';assert hashlib.sha256(path.read_bytes()).hexdigest()==prior['capture_audit_sha256']
    listing=json.loads(path.read_text());responses={}
    for p in listing['pages']:
        raw=(path.parent/p['file']).read_bytes();assert hashlib.sha256(raw).hexdigest()==p['sha256'];responses[p['url']]=raw.decode('utf-8')
    terminal=json.loads((source/'ens-lyon-terminal-boundary/audit.json').read_text());raw=(source/'ens-lyon-terminal-boundary'/terminal['file']).read_bytes()
    assert hashlib.sha256(raw).hexdigest()==terminal['sha256'];responses[terminal['url']]=raw.decode('utf-8')
    detail_path=source/'ens-lyon-detail-capture/audit.json';details=json.loads(detail_path.read_text())
    review=json.loads((source/'ens-lyon-all238-cursor-parser-independent-review.json').read_text());assert hashlib.sha256(detail_path.read_bytes()).hexdigest()==review['capture_audit_sha256']
    for url,r in details['responses'].items():
        raw=(detail_path.parent/r['file']).read_bytes();assert hashlib.sha256(raw).hexdigest()==r['sha256'] and r['status']==200;responses[url]=raw.decode('utf-8')
    os.environ['LIBERTREE_DB_BACKEND']='sqlite';conn=db_libertree.open_db(out/'owned.sqlite');db_libertree.init_db(conn);init_schema(conn)
    cls=CRAWLERS['ens-lyon-fr-lecole'];db_libertree.upsert_site(conn,cls.site_id,cls.site_name,cls.base_url)
    try:
        c=cls(conn,delay=0)
        c._request=lambda url,**kw:SimpleNamespace(text=responses[url])
        try:
            Collector(c,EnsLyonAdapter(c),Policy(full_check_days=0)).run()
        except ValueError as exc:
            assert 'repeated cross-page membership' in str(exc),str(exc)
            reason=str(exc)
        else:raise AssertionError('historical duplicate snapshot must not certify completeness')
        state=Store(conn,c.site_id).state();c._session.close()
        assert not state.get('baseline_complete',False)
        assert conn.execute('SELECT count(*) FROM documents').fetchone()[0]==0
        report=dict(status='passed_expected_snapshot_rejection',reviewed_at=utc_now(),reason=reason,
                    fixture_transport=True,live_http_requests=0,retained_memberships=239,
                    retained_unique_source_records=238,ambiguous_duplicate_ID='605900',
                    historical_snapshot_not_certified_complete=True,all_response_hashes_verified=True,
                    retained_checkpoint=state,source_documents_deleted=False,
                    operating_database_touched=False,global_source_complete=False)
        atomic_json(out/'review.json',report);print(json.dumps({k:v for k,v in report.items() if k!='retained_checkpoint'}));return report
    finally:conn.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--out',required=True)
    args=p.parse_args();run(args.source,args.out)
