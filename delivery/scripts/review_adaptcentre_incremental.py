"""Retained native WP catalogue/order; original HTML equivalence stays unverified."""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs,urlsplit

from crawler.incremental import Entry,observe_order,infer_order
from delivery.health.results import atomic_json,utc_now


def retained(source):
    source=Path(source);pages={};hashes={}
    for folder in ('sixteenth-public-adapt','sixteenth-adapt-boundaries','sixteenth-adapt-remaining32'):
        audit=json.loads((source/folder/'audit.json').read_text())
        for r in audit['responses']:
            if '/wp-json/wp/v2/publications?' not in r['url']:continue
            raw=(source/folder/r['file']).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=r['sha256']:raise ValueError('retained WP hash mismatch')
            page=int(parse_qs(urlsplit(r['url']).query)['page'][0])
            if page in pages:raise ValueError('duplicate retained page')
            pages[page]=json.loads(raw);hashes[str(Path(folder)/r['file'])]=r['sha256']
    if set(pages)!=set(range(1,37)):raise ValueError('retained WP page missing')
    end=pages[36]
    if end.get('code')!='rest_post_invalid_page_number' or end.get('data',{}).get('status')!=400:
        raise ValueError('retained explicit end missing')
    items=[i for p in range(1,36) for i in pages[p]]
    if len(items)!=3499 or len({i['id'] for i in items})!=3499 or len({i['link'] for i in items})!=3499:
        raise ValueError('retained native identity census changed')
    if any(len(pages[p])!=100 for p in range(1,35)) or len(pages[35])!=99:
        raise ValueError('retained page cardinality changed')
    return items,pages,hashes


def review(source,probe):
    items,pages,hashes=retained(source);probe=Path(probe)
    audit=json.loads((probe/'audit.json').read_text())
    if [r['page'] for r in audit['pages']]!=[1,35,36] or len(audit['requests'])!=3:
        raise ValueError('current bounded probe incomplete')
    for meta,r in zip(audit['pages'],audit['requests']):
        raw=(probe/r['body']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=r['sha256'] or json.loads(raw)!=pages[meta['page']]:
            raise ValueError('current boundary differs from retained scope')
        headers={k.lower():v for k,v in meta['headers'].items()}
        if meta['page']!=36 and (headers.get('x-wp-total')!='3499' or headers.get('x-wp-totalpages')!='35'):
            raise ValueError('current WP total differs')
    proof={};observe_order(proof,[Entry(str(i['id']),i,i['date']) for i in items],False)
    if infer_order(proof)!=('newest_first','date'):raise ValueError('native CMS-date chronology changed')
    acf=[(i.get('acf') or {}).get('date') for i in items]
    return dict(status='passed',reviewed_at=utc_now(),site_id='adaptcentre-ie-research',
        scope='existing native WordPress publications endpoint, all journal types',retained_native_metadata=3499,
        source_response_hashes=hashes,current_probe_sha256=hashlib.sha256((probe/'audit.json').read_bytes()).hexdigest(),
        actual_live_HTTP_requests=3,current_boundary_pages_match_retained=[1,35,36],source_CMS_order_proof=proof,
        source_CMS_order='newest_first',numeric_ID_ascending_steps=sum(b['id']>a['id'] for a,b in zip(items,items[1:])),
        publication_date_missing=sum(not d for d in acf),
        publication_date_ascending_steps=sum(bool(a and b and str(b)>str(a)) for a,b in zip(acf,acf[1:])),
        original_HTML_complete_ID_census_verified=False,original_HTML_conditional_count=3498,
        original_HTML_equivalence_verified=False,external_abstract_identity_verified=False,
        all_current_source_bodies_verified=False,global_source_complete=False,operating_database_touched=False)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--probe',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();out=Path(a.out)
    if out.exists():raise FileExistsError(out)
    report=review(a.source,a.probe);atomic_json(out,report)
    print(json.dumps({k:report[k] for k in ('status','retained_native_metadata','source_CMS_order','actual_live_HTTP_requests')}))
