"""Replay both original filtered ACPR routes without fetching details."""
import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from crawler.sites import CRAWLERS
from crawler.incremental_acpr import ACPRAdapter
from crawler.incremental import observe_order,infer_order
from delivery.health.results import atomic_json,utc_now


def run(source,out):
    source=Path(source);root=source/'acpr-delivery-scope-observations-v2';audit_path=root/'audit.json'
    audit=json.loads(audit_path.read_text());prior=json.loads((source/'acpr-all-delivery-enumeration-independent-review.json').read_text())
    manifest=source/'acpr-all-delivery-source-records.jsonl';assert hashlib.sha256(manifest.read_bytes()).hexdigest()==prior['source_manifest_sha256']
    assert hashlib.sha256(audit_path.read_bytes()).hexdigest()==prior['audit_sha256']
    expected=[json.loads(x) for x in manifest.read_text().splitlines()];c=CRAWLERS['acpr-banque-france-fr-fr'](None,delay=0);helpers=c.crawl.__globals__;responses={}
    for page in audit['pages']:
        raw=(root/page['file']).read_bytes();assert hashlib.sha256(raw).hexdigest()==page['sha256']
        responses[helpers['_list_url'](page['scope'],page['page'])]=raw.decode('utf-8',errors='replace')
    c._request=lambda url,**kw:SimpleNamespace(text=responses[url]);c._assert_identity=lambda item:None
    a=ACPRAdapter(c)
    for page in audit['pages']:assert a.read(page['scope'],page['page'])['items']==page['items']
    entries=[];cursor=None;proof={}
    while True:
        page=a.fetch(cursor,'native');entries.extend(page.entries);observe_order(proof,page.entries,False);cursor=page.next_cursor
        if page.exhausted:break
    assert len(entries)==len({e.key for e in entries})==733
    by_url={r['url']:r for r in expected};assert {e.payload['url'] for e in entries}==set(by_url)
    for entry in entries:
        row=by_url[entry.payload['url']];assert all(entry.payload[k]==v for k,v in row.items() if k not in ('scope','page'));assert entry.payload['_source_scope']==row['scope']
    assert infer_order(proof)==('newest_first','date')
    report={'status':'passed','reviewed_at':utc_now(),'retained_pages':62,'source_records':733,
            'manifest_sha256':prior['source_manifest_sha256'],'all_response_hashes_verified':True,
            'all_native_payloads_equal':True,'both_delivered_scopes_and_original_study_filters_preserved':True,
            'scope_counts':{s:sum(e.payload['_source_scope']==s for e in entries) for s in ('studies','press')},
            'merged_order':infer_order(proof),'proof':proof,'global_source_complete':False,'actual_body_and_file_completion_not_reverified':True}
    atomic_json(Path(out),report);print(json.dumps({k:v for k,v in report.items() if k!='proof'}));return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();run(a.source,a.out)
