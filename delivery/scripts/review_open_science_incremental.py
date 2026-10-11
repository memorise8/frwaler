"""Verify sorted original-community membership against retained score queries."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl,urlencode,urlsplit,urlunsplit

from crawler.sites import CRAWLERS
from crawler.incremental_open_science import OpenScienceAdapter
from crawler.incremental import observe_order,infer_order
from delivery.health.results import atomic_json,utc_now


def key(url):
    p=urlsplit(url);return urlunsplit((p.scheme,p.netloc,p.path,urlencode(sorted(parse_qsl(p.query))),''))


def run(source,capture,out):
    p=Path(source);capture=Path(capture);root=p/'open-science-delivery-scope-enumeration'
    original=json.loads((root/'audit.json').read_text());prior=json.loads((p/'open-science-delivery-enumeration-independent-review.json').read_text())
    assert hashlib.sha256((root/'audit.json').read_bytes()).hexdigest()==prior['source_audit_sha256']
    c=CRAWLERS['open-science-canada-ca-communities'](None,delay=0);old={};types=Counter()
    for page in original['pages']:
        raw=(root/page['file']).read_bytes();assert hashlib.sha256(raw).hexdigest()==page['sha256'];data=json.loads(raw)
        items=[c._indexable_object(x) for x in data['_embedded']['searchResult']['_embedded']['objects']]
        old.setdefault(page['scope'],{}).update({x['uuid']:x['type'] for x in items})
    audit=json.loads((capture/'audit.json').read_text());assert audit['status']=='captured';cache={}
    for page in audit['pages']:
        r=audit['requests'][page['request']];raw=(capture/r['body']).read_bytes()
        assert r['status']==200 and hashlib.sha256(raw).hexdigest()==r['sha256'];cache[key(r['url'])]=json.loads(raw)
    def request(url,params=None,**kw):
        if params:url+='?'+urlencode(params)
        return SimpleNamespace(json=lambda:cache[key(url)])
    c._request=request;c._stored_cursor_item=lambda item:False
    a=OpenScienceAdapter(c,rows=100);new={};proofs={}
    for page in audit['pages']:
        items,total,pages,digest=a.read(page['scope'],page['page']);scope=page['scope']
        assert len(items)==min(100,total-page['page']*100)
        for item in items:
            assert item['uuid'] not in new.setdefault(scope,{})
            new[scope][item['uuid']]=item['type'];types[item['type']]+=1
    assert old==new
    entries=[];cursor=None;proof={};calls=0
    while True:
        page=a.fetch(cursor,'native');entries.extend(page.entries);cursor=page.next_cursor;calls+=1
        observe_order(proof,page.entries,False)
        if page.exhausted:break
        assert calls<30
    assert len(entries)==len({e.key for e in entries})==944
    assert infer_order(proof)==('newest_first','date')
    report={'status':'passed','reviewed_at':utc_now(),'original_pages_verified':39,'sorted_pages_verified':len(audit['pages']),
            'same_entity_ID_type_memberships_by_scope':True,'scope_entities':{s:len(v) for s,v in new.items()},
            'entity_types':dict(types),'merged_documents':len(entries),'full_merged_order':infer_order(proof),'proof':proof,
            'source_scope_or_query_not_substituted':True,'source_sort':'dc.date.accessioned,DESC','global_source_complete':False,
            'detail_body_and_file_bytes_not_reverified_by_catalogue_review':True}
    atomic_json(Path(out),report);print(json.dumps({k:v for k,v in report.items() if k!='proof'}));return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--capture',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();run(a.source,a.capture,a.out)
