"""Reconcile retained source pages for ordering holds without new source requests."""
import argparse
from collections import Counter,defaultdict
import hashlib
import json
from pathlib import Path
import re

from bs4 import BeautifulSoup
from crawler.incremental import Entry,observe_order,infer_order
from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json,utc_now


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def run(source,out):
    source=Path(source);out=Path(out);out.mkdir(parents=True,exist_ok=False)
    report={'reviewed_at':utc_now(),'source_root':str(source),'live_network_requests':0,'sites':{}}
    manifest=source/'amolf-all7593-source-memberships.jsonl'
    rows=[json.loads(line) for line in manifest.open()];pages=defaultdict(list)
    for row in rows:pages[row['page']].append(row)
    checks=[];proof={};missing=0;examples=[]
    for page,records in sorted(pages.items()):
        path=source/records[0]['source_file'];digest=sha(path)
        assert all(r['source_sha256']==digest for r in records)
        soup=BeautifulSoup(path.read_bytes(),'lxml')
        links=soup.select('h2 a[href*="/publications/"]')
        assert [a['href'] for a in links]==[r['url'] for r in records]
        entries=[]
        for a,row in zip(links,records):
            container=a.parent.parent
            full_date=container.select_one('[datetime]')
            value=full_date.get('datetime','') if full_date else ''
            # Citation years do not certify insertion order within that year.
            if not value:
                missing+=1
                if len(examples)<3:examples.append({'page':page,'url':row['url'],'citation':row['citation_reference_raw']})
            entries.append(Entry(row['url'],{},value))
        observe_order(proof,entries,False)
        checks.append({'page':page,'sha256':digest,'rows':len(records)})
    assert len(checks)==380 and len(rows)==7593
    report['sites']['amolf-nl-publications']={'source_sha256':sha(Path(CRAWLERS['amolf-nl-publications'].crawl.__code__.co_filename)),
        'retained_pages':checks,'source_manifest_sha256':sha(manifest),'memberships':len(rows),
        'unique_url_count':len({r['url'] for r in rows}),'rows_without_list_datetime':missing,
        'numeric_list_ids':0,'order_proof':proof,'inferred_order':infer_order(proof),
        'examples':examples,'native_list_selector':'li.list__item--publication',
        'reason':'list_datetime_absent_and_url_slugs_not_chronological_ids',
        'classification':'full_rescan_required','native_parser_health':'obsolete_list_selectors_separate_repair_required'}
    root=source/'krivet-delivery-menu-enumeration-v3';audit_path=root/'audit.json'
    audit=json.loads(audit_path.read_text());c=CRAWLERS['krivet-re-kr-kor'](None,delay=0)
    summaries={};checks=[];previous={};inversions=defaultdict(list);counts=Counter();id_formats=Counter()
    for page in sorted(audit['pages'],key=lambda r:(r['menu'],r['page'])):
        path=root/page['file'];digest=sha(path);assert digest==page['sha256']
        c._active_menu=page['menu'];items=c._parse_list_page(path.read_text())
        assert [(r['pst_no'],r['source_ordinal']) for r in items]==[(r['pst_no'],r['source_ordinal']) for r in page['records']]
        proof=summaries.setdefault(page['menu'],{});entries=[]
        for item in items:
            value=(item['listed_raw'] or item['published_raw']).replace('.','-')
            uid=item['pst_no'];entries.append(Entry(uid,{},value));counts[page['menu']]+=1
            id_formats[re.sub(r'\d+','N',uid)]+=1
            prior=previous.get(page['menu'])
            if prior and value>prior['date'] and len(inversions[page['menu']])<5:
                inversions[page['menu']].append({'before':prior,'after':{'page':page['page'],'id':uid,'date':value}})
            previous[page['menu']]={'page':page['page'],'id':uid,'date':value}
        observe_order(proof,entries,False)
        checks.append({'menu':page['menu'],'page':page['page'],'sha256':digest,'rows':len(items)})
    assert len(checks)==874 and sum(counts.values())==8715
    report['sites'][c.site_id]={'source_sha256':sha(Path(c.crawl.__code__.co_filename)),
        'retained_pages':checks,'source_audit_sha256':sha(audit_path),'memberships_by_menu':dict(counts),
        'list_id_formats':dict(id_formats),'order_proof_by_menu':summaries,
        'inferred_order_by_menu':{m:infer_order(p) for m,p in summaries.items()},
        'date_inversion_examples':dict(inversions),'classification':'full_rescan_required',
        'reason':'menus_15_12_visible_dates_nonmonotonic_and_opaque_post_ids',
        'partial_candidates':['19','23'],
        'scope_note':'Do not certify all four delivered menus from the two chronological menus. Source ordinals are mutable row positions, not stable IDs.'}
    report['status']='passed_retained_source_order_review';atomic_json(out/'order-review.json',report)
    print(json.dumps({s:{k:v for k,v in d.items() if k in ('inferred_order','inferred_order_by_menu','rows_without_list_datetime','reason','memberships_by_menu')} for s,d in report['sites'].items()}))
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();run(a.source,a.out)
