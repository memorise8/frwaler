"""Review undated source cards without borrowing dates/IDs from one detail sample."""
import argparse
import hashlib
import json
from pathlib import Path

from bs4 import BeautifulSoup
from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json,utc_now

SITE='ameslab-gov-news'
REASON='listing_dates_and_node_IDs_detail_only_with_two_extra_native_categories_unreviewed'


def retained(source):
    source=Path(source);ledger=json.loads((source/'site-progress.json').read_text())
    facts=next(r for r in ledger['sites'] if r['site_id']==SITE)['source_current_named_catalogue_facts']
    def checked(name,sha):
        raw=(source/name).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('Ames source evidence changed')
        return raw
    memberships=[json.loads(s) for s in checked(facts['membership_file'],facts['membership_sha256']).decode().splitlines()]
    expected={r['url']:r for r in memberships};c=CRAWLERS[SITE](None,delay=0);pages=[];items=[]
    try:
        for index,p in enumerate(facts['pages']):
            raw=checked(p['source_file'],p['source_sha256']);url=c._list_url('news-releases',index)
            rows=c._parse_list_page(raw.decode(),url,'News Release');soup=BeautifulSoup(raw,'html.parser');cards=soup.select('article.card')
            if len(rows)!=p['memberships'] or len(rows)!=len(cards):raise ValueError('native original memberships changed')
            for card in cards:
                if card.select('time,[datetime],[data-history-node-id]') or card.has_attr('data-history-node-id'):
                    raise ValueError('new source date/identity requires renewed review')
            for r in rows:
                m=expected[r['url']]
                if r['title']!=m['title'] or r['list_snippet']!=m['snippet']:raise ValueError('native list fields changed')
            items+=rows;pages.append(dict(url=url,raw=raw,file=p['source_file'],sha256=p['source_sha256']))
        if len(items)!=102 or len({i['url'] for i in items})!=102 or len(pages)!=12:
            raise ValueError('original 102-row catalogue changed')
        audit=json.loads((source/'seventeenth-ames/audit.json').read_text())
        detail=next(r for r in audit['responses'] if r['scope']=='first-detail')
        detail_raw=checked('seventeenth-ames/'+detail['file'],detail['sha256'])
        if detail['url']!=items[0]['url']:raise ValueError('first detail identity changed')
        c._curl_head_content_disposition=lambda url:(_ for _ in ()).throw(ValueError('uncaptured HEAD forbidden'))
        paper=c._build_paper(items[0],c._parse_detail_page(detail_raw.decode(),items[0]['url'],'News Release'))
        if not paper['external_id'].isdecimal() or not paper['published_date']:raise ValueError('actual first detail identity/date unavailable')
        return pages,items,dict(raw=detail_raw,paper=paper,file='seventeenth-ames/'+detail['file'],sha256=detail['sha256'])
    finally:c._session.close()


def review(source,probe):
    pages,items,detail=retained(source);probe=Path(probe);audit=json.loads((probe/'audit.json').read_text())
    if len(audit['requests'])!=1:raise ValueError('unexpected actual request count')
    r=audit['requests'][0];raw=(probe/r['body']).read_bytes();c=CRAWLERS[SITE](None,delay=0)
    try:
        if (r['url']!=pages[0]['url'] or r['status']!=200 or hashlib.sha256(raw).hexdigest()!=r['sha256']
                or c._parse_list_page(raw.decode(),r['url'],'News Release')!=items[:10]):
            raise ValueError('current first-page fields changed')
    finally:c._session.close()
    code=Path(__file__).resolve().parents[2]
    return dict(status='passed',reviewed_at=utc_now(),site_id=SITE,classification='full_rescan_required',reason=REASON,
        original_scope_url=pages[0]['url'],retained_source_memberships=102,undated_source_cards=102,
        list_node_IDs_unavailable=True,actual_retained_detail_bodies=1,actual_first_detail_ID=detail['paper']['external_id'],
        actual_first_detail_date=detail['paper']['published_date'],other101_actual_details_unverified=True,
        current_first_page_fields_match=10,actual_live_HTTP_requests=1,
        extra_native_categories=[s for s,_ in CRAWLERS[SITE]._CATEGORIES if s!='news-releases'],
        all_native_scope_equivalence_verified=False,source_order=None,
        source_response_hashes={p['file']:p['sha256'] for p in pages},actual_detail_hash={detail['file']:detail['sha256']},
        current_probe_sha256=hashlib.sha256((probe/'audit.json').read_bytes()).hexdigest(),
        source_sha256=hashlib.sha256(Path(CRAWLERS[SITE].crawl.__code__.co_filename).read_bytes()).hexdigest(),
        dependency_source_sha256={'delivery/health/access_challenge.py':hashlib.sha256((code/'delivery/health/access_challenge.py').read_bytes()).hexdigest()},
        baseline_complete=False,global_source_complete=False,all_files_verified=False,operating_database_touched=False)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--probe',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();out=Path(a.out)
    if out.exists():raise FileExistsError(out)
    r=review(a.source,a.probe);atomic_json(out,r);print({k:r[k] for k in ('status','retained_source_memberships','actual_first_detail_ID')})
