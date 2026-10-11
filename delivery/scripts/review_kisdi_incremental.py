"""Replay retained KISDI list pages through the connected merge adapter."""
import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from crawler.sites import CRAWLERS
from crawler.incremental_kisdi import KisdiAdapter
from crawler.incremental import observe_order,infer_order
from delivery.health.results import atomic_json,utc_now


def run(source,out):
    p=Path(source);root=p/'kisdi-delivery-scope-observations'
    audit_path=root/'audit.json';audit=json.loads(audit_path.read_text())
    prior=json.loads((p/'kisdi-all-delivery-enumeration-independent-review.json').read_text())
    manifest=p/'kisdi-all-delivery-source-records.jsonl'
    assert hashlib.sha256(manifest.read_bytes()).hexdigest()==prior['source_manifest_sha256']
    assert hashlib.sha256(audit_path.read_bytes()).hexdigest()==prior['audit_sha256']
    expected=[json.loads(x) for x in manifest.read_text().splitlines()]
    c=CRAWLERS['kisdi-re-kr-report'](None,delay=0);helpers=c.crawl.__globals__;byurl={}
    for page in audit['pages']:
        raw=(root/page['file']).read_bytes();assert hashlib.sha256(raw).hexdigest()==page['sha256']
        byurl[helpers['_list_url'](page['scope'],page['page'])]=raw.decode('utf-8')
    c._request=lambda url,**kw:SimpleNamespace(text=byurl[url])
    c._assert_identity=lambda item:None # no database assertion claimed by this list-only replay
    a=KisdiAdapter(c)
    for page in audit['pages']:
        assert a.read(page['scope'],page['page'])['items']==page['items']
    cursor=None;entries=[];proof={}
    while True:
        page=a.fetch(cursor,'native');entries.extend(page.entries);observe_order(proof,page.entries,False);cursor=page.next_cursor
        if page.exhausted:break
    assert len(entries)==len(expected)==1274 and len({e.key for e in entries})==1274
    by_id={r['art_id']:r for r in expected};assert {e.key for e in entries}==set(by_id)
    for entry in entries:
        row=by_id[entry.key]
        assert all(entry.payload[k]==v for k,v in row.items() if k not in ['scope','page'])
        assert entry.payload['_scope']==row['scope']
    assert infer_order(proof)==('newest_first','date')
    scopes={}
    for scope in a.scope['scopes']:
        rows=[x for x in expected if x['scope']==scope];dates=[x['published_date_raw'] for x in rows];ids=[int(x['art_id']) for x in rows]
        scopes[scope]={'records':len(rows),'date_up':sum(a<b for a,b in zip(dates,dates[1:])),
            'date_down':sum(a>b for a,b in zip(dates,dates[1:])),
            'id_up':sum(a<b for a,b in zip(ids,ids[1:])),'id_down':sum(a>b for a,b in zip(ids,ids[1:]))}
    report={'status':'passed','reviewed_at':utc_now(),'retained_pages':len(audit['pages']),'records':1274,
            'manifest_sha256':prior['source_manifest_sha256'],'all_response_hashes_verified':True,
            'all_native_list_payloads_equal':True,'merged_order':infer_order(proof),'merged_proof':proof,
            'scope_proofs':scopes,'global_source_complete':False,'detail_body_completion_not_certified':True}
    atomic_json(Path(out),report);print(json.dumps(scopes));return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--out',required=True)
    args=p.parse_args();run(args.source,args.out)
