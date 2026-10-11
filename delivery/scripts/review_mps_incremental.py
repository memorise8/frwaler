"""Preserve original filtered scopes and sampled native order uncertainty."""
import argparse
import hashlib
import json
from pathlib import Path

from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json,utc_now

SITE='app-mps-gov-cn-gdnps'
REASON='native_unfiltered_scope_equivalence_and_incomplete_scrq_order_unverified'


def retained(source):
    source=Path(source);g=CRAWLERS[SITE].crawl.__globals__
    review=json.loads((source/'eighteenth-eight-followups-independent-review.json').read_text());pages={};groups={}
    for r in review['rows']:
        if r['site_id']!=SITE or not (r['scope'].startswith('original-') or r['scope'].startswith('exact-native')):continue
        raw=(source/r['source_file']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=r['source_sha256']:raise ValueError('retained API response changed')
        data=g['_parse_jsonp'](raw.decode());rows=data['resultMap']
        if len(rows)!=r['memberships']:raise ValueError('source membership changed')
        pages[r['scope']]=dict(raw=raw,data=data,items=rows,evidence=r)
        if 'detail' not in r['scope'] and r['scope'].startswith('original-'):
            name=r['scope'].replace('-first','').split('-page-')[0];groups.setdefault(name,[]).extend(rows)
    if {k:len(v) for k,v in groups.items()}!={'original-annual':18,'original-policy-regulations':63,'original-policy-normative':49}:
        raise ValueError('original group sizes changed')
    if len({str(r['id']) for v in groups.values() for r in v})!=130:raise ValueError('original ID membership changed')
    native=pages['exact-native-unfiltered-first']
    if int(native['data']['totalContentNum'])!=2626 or len(native['items'])!=15:
        raise ValueError('native sample scope changed')
    if sum(not r.get('scrq') for r in native['items'])!=3:raise ValueError('native missing sort-key evidence changed')
    c=CRAWLERS[SITE](None,delay=0)
    try:
        if c._list_url(1)!=native['evidence']['url']:raise ValueError('native query changed')
    finally:c._session.close()
    return pages,groups


def review(source,probe):
    pages,groups=retained(source);probe=Path(probe);audit=json.loads((probe/'audit.json').read_text());g=CRAWLERS[SITE].crawl.__globals__
    if len(audit['requests'])!=1:raise ValueError('unexpected live request count')
    r=audit['requests'][0];raw=(probe/r['body']).read_bytes();data=g['_parse_jsonp'](raw.decode())
    native=pages['exact-native-unfiltered-first']
    # The API includes generated/transient fields. Compare the native first
    # IDs and materialization inputs, rather than requiring byte equality.
    keys=('id','title','scrq','publishTime','nrgs','zz','fbjg','ztfl','zpfl','subjectName','syh','ownSubjectDn','ownSubjectId')
    project=lambda rows:[{k:x.get(k) for k in keys} for x in rows]
    if (r['status']!=200 or r['url']!=native['evidence']['url'] or hashlib.sha256(raw).hexdigest()!=r['sha256']
            or project(data['resultMap'])!=project(native['items']) or data['totalContentNum']!=native['data']['totalContentNum']):
        raise ValueError('current native membership changed')
    order={}
    for name,rows in groups.items():
        order[name]={}
        for key in ('scrq','publishTime','id'):
            values=[str(x.get(key) or '') for x in rows];pairs=[(a,b) for a,b in zip(values,values[1:]) if a and b]
            order[name][key]=dict(missing=sum(not x for x in values),ascending_pairs=sum(a<b for a,b in pairs),descending_pairs=sum(a>b for a,b in pairs),comparison='source string values; IDs not asserted chronological')
    code=Path(__file__).resolve().parents[2];cls=CRAWLERS[SITE]
    return dict(status='passed',reviewed_at=utc_now(),site_id=SITE,reason=REASON,classification='full_rescan_required',
        original_filtered_unique_IDs=130,original_group_counts={k:len(v) for k,v in groups.items()},native_reported_total=2626,
        native_first_missing_scrq=3,original_order_evidence=order,full_scope_equivalence_verified=False,
        all2626_IDs_enumerated=False,current_native_first_fields_match=15,actual_live_HTTP_requests=1,
        source_response_hashes={p['evidence']['source_file']:p['evidence']['source_sha256'] for p in pages.values()},
        current_probe_sha256=hashlib.sha256((probe/'audit.json').read_bytes()).hexdigest(),
        source_sha256=hashlib.sha256(Path(cls.crawl.__code__.co_filename).read_bytes()).hexdigest(),
        dependency_source_sha256={'delivery/health/access_challenge.py':hashlib.sha256((code/'delivery/health/access_challenge.py').read_bytes()).hexdigest()},
        baseline_complete=False,global_source_complete=False,all_files_verified=False,operating_database_touched=False)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--probe',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();out=Path(a.out)
    if out.exists():raise FileExistsError(out)
    r=review(a.source,a.probe);atomic_json(out,r);print({'status':r['status'],'original_IDs':130,'native_total':2626})
