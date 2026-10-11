"""Recheck retained original ALRC memberships, all dates and adjacent end proofs."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path

import requests
from crawler.alrc_scope import SCOPES,list_url
from crawler.incremental_alrc import ALRCAdapter
from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json,utc_now

SITE='alrc-gov-au-publications'


def retained(source):
    source=Path(source);prior=json.loads((source/'alrc-all-delivered-catalogues-independent-review.json').read_text())
    def checked(name,sha):
        raw=(source/name).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('ALRC retained evidence hash changed')
        return raw
    rows=[json.loads(s) for s in checked(prior['listing_records'],prior['listing_records_sha256']).decode().splitlines()]
    audit=json.loads(checked('alrc-delivered-catalogues/audit.json',prior['source_audit_sha256']))
    pages=defaultdict(list);responses={};hashes={}
    for row in rows:pages[(row['scope'],row['page'])].append(row)
    for (scope,page),members in pages.items():
        row=members[0];raw=checked(row['source_file'],row['source_sha256'])
        responses[list_url(scope,page)]=(raw,200);hashes[row['source_file']]=row['source_sha256']
    for end in audit['boundaries']:
        r=end['response'];name='alrc-delivered-catalogues/'+r['file'];raw=checked(name,r['sha256'])
        responses[r['url']]=(raw,r['response']['status']);hashes[name]=r['sha256']
    c=CRAWLERS[SITE](None,delay=0);adapter=ALRCAdapter(c)
    def transport(method,url,**kwargs):
        raw,status=responses[url];r=requests.Response();r.url=url;r.status_code=status;r._content=raw;r.encoding='utf-8';return r
    c._session.request=transport;all_items={};scope_dates={};proof=[]
    try:
        for scope in SCOPES:
            collected=[]
            for page in range(1,prior['source_pages'][scope]+1):
                items,last,_=adapter.read(scope,page)
                if last!=prior['source_pages'][scope]:raise ValueError('ALRC source last page changed')
                expected=pages[(scope,page)]
                actual=[{k:i.get(k) for k in ('url','title','listed_date_raw','type_label')} for i in items]
                normalized=[{k:i.get(k) for k in actual[0]} for i in expected]
                for i in normalized:i['title']=c._alrc_paper.__globals__['_clean_text'](i['title'])
                if actual!=normalized:raise ValueError('ALRC original list metadata changed')
                collected+=items
                for i in items:all_items.setdefault(i['url'],i)
            dates=[i['listed_date'] for i in collected];scope_dates[scope]=dates
            if dates!=sorted(dates,reverse=True):raise ValueError('ALRC retained chronology changed')
            proof.append(dict(scope=scope,rows=len(collected),first=dates[0],last=dates[-1],
                decreasing_pairs=sum(a>b for a,b in zip(dates,dates[1:])),increasing_pairs=0,missing=0))
        if len(rows)!=947 or len(all_items)!=927:raise ValueError('ALRC scope union changed')
        return responses,all_items,dict(source_memberships=947,unique_source_URLs=927,scope_overlap=20,
            scope_order_proof=proof,source_hashes=hashes,original_scope_urls=[r['url'] for r in prior['delivered_rows']],
            original_csv_sha256=prior['delivery_csv_sha256'])
    finally:c._session.close()


def review(source,probe):
    responses,items,facts=retained(source);probe=Path(probe);audit=json.loads((probe/'audit.json').read_text())
    cls=CRAWLERS[SITE];parse=cls._alrc_paper.__globals__['_parse_list_page']
    if len(audit['requests'])!=2 or audit.get('error_type'):raise ValueError('current two-scope probe incomplete')
    for r in audit['requests']:
        raw=(probe/r['body']).read_bytes()
        if r['status']!=200 or hashlib.sha256(raw).hexdigest()!=r['sha256'] or r['url'] not in [list_url(s,1) for s in SCOPES]:
            raise ValueError('current source first page changed')
        if parse(raw.decode())!=parse(responses[r['url']][0].decode()):raise ValueError('current native list fields differ')
    code=Path(__file__).resolve().parents[2]
    deps=['crawler/incremental_alrc.py','crawler/sites/custom/alrc-gov-au-publications.py','crawler/alrc_scope.py',
          'delivery/health/access_challenge.py','docs/deliverables/test_URL_수집대조_20260802.csv']
    if hashlib.sha256((code/deps[-1]).read_bytes()).hexdigest()!=facts['original_csv_sha256']:raise ValueError('original CSV changed')
    return dict(status='passed',reviewed_at=utc_now(),site_id=SITE,**facts,actual_live_HTTP_requests=2,
        current_two_first_pages_native_fields_match=True,retained_populated_pages=96,retained_adjacent_end_responses=2,
        source_sha256=hashlib.sha256(Path(cls.crawl.__code__.co_filename).read_bytes()).hexdigest(),
        dependency_source_sha256={p:hashlib.sha256((code/p).read_bytes()).hexdigest() for p in deps},
        current_probe_sha256=hashlib.sha256((probe/'audit.json').read_bytes()).hexdigest(),
        global_source_complete=False,all_actual_detail_bodies_verified=False,all_files_verified=False,operating_database_touched=False)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--probe',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();out=Path(a.out)
    if out.exists():raise FileExistsError(out)
    r=review(a.source,a.probe);atomic_json(out,r);print({k:r[k] for k in ('status','source_memberships','unique_source_URLs')})
