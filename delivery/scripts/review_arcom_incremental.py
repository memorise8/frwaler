"""Compare retained ASC and current DESC press catalogues without merging archives."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

from crawler.incremental_arcom import ARCOMAdapter
from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json, utc_now

SITE='arcom-fr-presse'
PRIOR_COMMIT='fd754d0db1d6edd4345136c5c9603b3fe0f6a48f'
PRIOR_SHA='00e6110cfe2d1e208a5d8eeb03fd82bc4274adacb48d38bcc03a213e90564ea5'


def retained(source, batch):
    source=Path(source);batch=Path(batch);c=CRAWLERS[SITE](None,delay=0);a=ARCOMAdapter(c)
    audit=json.loads((source/'arcom-fr-presse-exact-delivered-entries/audit.json').read_text())
    follow=json.loads((source/'eighteenth-eight-followups-independent-review.json').read_text())
    hashes={};old=[];pages={};allitems=[];actual={}
    def checked(root,name,sha):
        raw=(root/name).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('ARCOM source evidence changed')
        hashes[str(root/name)]=sha
        return raw
    def parse_ascending(raw):
        soup=c.crawl.__globals__['_make_soup'](raw);items=[]
        for card in soup.select('.view-id-presse .views-row'):
            link=card.select_one('.card-title a');stamp=c.crawl.__globals__['_parse_french_date'](card.select_one('.card-infos .tag-list').get_text(' ',strip=True))
            items.append(dict(url=c.base_url+link['href'],title=link.get_text(' ',strip=True),date=stamp))
        return items
    try:
        original=audit['responses'][0]
        if original['url']!=c._LIST_BASE:raise ValueError('original press scope changed')
        old+=parse_ascending(checked(source,'arcom-fr-presse-exact-delivered-entries/'+original['file'],original['sha256']))
        for row in follow['rows']:
            if row['site_id']!=SITE:continue
            if row['scope'].startswith('original-page-'):
                raw=checked(source,row['source_file'],row['source_sha256']);parsed=parse_ascending(raw)
                if len(parsed)!=row['memberships']:raise ValueError('ASC membership changed')
                old+=parsed
            elif row['scope']=='first-detail':
                actual=dict(url=row['url'],raw=checked(source,row['source_file'],row['source_sha256']),evidence=row)
        if len(old)!=310 or len({i['url'] for i in old})!=309 or [i['date'] for i in old]!=sorted(i['date'] for i in old):
            raise ValueError('original ASC repetition/order changed')
        for name in ['canonical-probe','descending-pages']:
            probe=json.loads((batch/name/'audit.json').read_text())
            if probe['status']!='passed':raise ValueError('current probe incomplete')
            for row in probe['requests']:
                raw=checked(batch/name,row['body'],row['sha256'])
                if row['status']!=200:raise ValueError('current source denied')
                if '_DESC' not in row['url']:
                    current=parse_ascending(raw)
                    if ({i['url']:i for i in current}!={i['url']:i for i in old[:50]} or [i['date'] for i in current]!=[i['date'] for i in old[:50]]):raise ValueError('current ASC first page changed')
                    continue
                page=a.page_number(row['url']);items,nxt,total=a.inspect(raw.decode(),page,empty_after=6 if page==7 else None)
                if total!=310:raise ValueError('current source total changed')
                pages[page]=dict(raw=raw,items=items,url=row['url'])
        if set(pages)!=set(range(8)):raise ValueError('DESC traversal incomplete')
        allitems=[i for n in range(7) for i in pages[n]['items']];items={i['url']:i for i in allitems}
        if len(allitems)!=310 or len(items)!=310 or [i['date'] for i in allitems]!=sorted((i['date'] for i in allitems),reverse=True):
            raise ValueError('DESC distinct count or chronology changed')
        oldmap={i['url']:i for i in old};extra=set(items)-set(oldmap)
        if len(extra)!=1 or set(oldmap)-set(items):raise ValueError('ASC/DESC scope relation changed')
        for url,item in oldmap.items():
            if any(items[url][k]!=item[k] for k in ('title','date')):raise ValueError('shared ASC/DESC fields changed')
        url=actual['url'];slug=url.rsplit('/',1)[-1];paper=c._fetch_detail(slug,url,html=actual['raw'].decode())
        root=Path(__file__).resolve().parents[2]
        old_code=subprocess.check_output(['git','show',PRIOR_COMMIT+':crawler/sites/custom/arcom-fr-presse.py'],cwd=root)
        if hashlib.sha256(old_code).hexdigest()!=PRIOR_SHA:raise ValueError('prior native changed')
        ns={'__name__':'arcom_native_before'};exec(compile(old_code,'arcom_native_before','exec'),ns)
        before=ns['ArcomFrPresseCrawler'](None,delay=0)
        try:
            with patch.dict(ns,{'_curl_get':lambda u:actual['raw'].decode()}):prior=before._fetch_detail(slug,url)
        finally:before._session.close()
        if not paper or paper!=prior:raise ValueError('native real detail fields changed')
        actual['paper']=paper
        return pages,items,actual,dict(retained_ASC_memberships=310,retained_ASC_unique_URLs=309,
            actual_DESC_memberships=310,actual_DESC_unique_URLs=310,ASC_repeat_not_inferred_two_documents=True,
            additional_DESC_URLs=sorted(extra),all_shared_309_list_fields_match=True,source_hashes=hashes,
            original_current_press_scope=True,older_archive_scope_included=False,source_order='newest_first',
            first_date=allitems[0]['date'],last_date=allitems[-1]['date'],actual_detail_papers_preserved=1,current_ASC_same_date_internal_reordering=True)
    finally:c._session.close()


def review(source,batch):
    pages,items,actual,facts=retained(source,batch);root=Path(__file__).resolve().parents[2];cls=CRAWLERS[SITE]
    paths=['crawler/incremental_arcom.py','delivery/health/access_challenge.py','crawler/document_dates.py']
    return dict(status='passed',site_id=SITE,reviewed_at=utc_now(),**facts,actual_live_HTTP_requests=10,
        initial_page_zero_redirect=301,canonical_page_zero_used=True,
        source_sha256=hashlib.sha256(Path(cls.crawl.__code__.co_filename).read_bytes()).hexdigest(),
        dependency_source_sha256={p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in paths},
        baseline_complete=False,global_source_complete=False,all310_actual_details_verified=False,
        all_files_verified=False,operating_database_touched=False)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--batch',required=True);p.add_argument('--out',required=True)
    args=p.parse_args();out=Path(args.out)
    if out.exists():raise FileExistsError(out)
    result=review(args.source,args.batch);atomic_json(out,result);print({k:result[k] for k in ('status','actual_DESC_unique_URLs','retained_ASC_unique_URLs')})
