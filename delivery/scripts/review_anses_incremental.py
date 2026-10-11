"""Review sampled date contradictions and original scope exclusions, not 6199 PDFs."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json,utc_now

SITE='anses-fr-fr'
REASON='sampled_signature_and_online_dates_nonmonotonic_and_two_original_scopes_unconfigured'
CATALOGUE='docs/deliverables/test_URL_수집대조_20260802.csv'


def ordering(rows,key):
    dates=[r[key] for r in rows];pairs=[(a,b) for a,b in zip(dates,dates[1:]) if a and b]
    return dict(missing=sum(not d for d in dates),ascending_pairs=sum(a<b for a,b in pairs),
                descending_pairs=sum(a>b for a,b in pairs),equal_pairs=sum(a==b for a,b in pairs))


def retained(source):
    source=Path(source);code=Path(__file__).resolve().parents[2];c=CRAWLERS[SITE](None,delay=0)
    def checked(file,sha):
        raw=(source/file).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('retained source changed')
        return raw
    try:
        audit=json.loads((source/'anses-fr-fr-exact-delivered-entries/audit.json').read_text())
        with (code/CATALOGUE).open(encoding='utf-8-sig',newline='') as f:
            urls={r['URL주소'] for r in csv.DictReader(f) if r['매칭크롤러(site_id)']==SITE}
        if urls!={r['url'] for r in audit['source_scope_rows']} or c.START_URL not in urls or len(urls)!=3:
            raise ValueError('original scope changed')
        prior=json.loads((source/'eighteenth-native-scope-and-detail-review.json').read_text())['results']['ANSES']
        pages={}
        for r in prior['actual_first_three_list_parsers']:
            raw=checked(r['source_file'],r['source_sha256']);rows=c._parse_list(c._make_soup(raw))
            if rows!=r['native_records']:raise ValueError('native first-scope fields changed')
            scope='scope-'+str(r['original_scope'])
            url=next(x['url'] for x in audit['terminal_scopes'] if x['scope']==scope)
            pages[scope]=dict(raw=raw,items=rows,file=r['source_file'],sha256=r['source_sha256'],url=url)
        follow=json.loads((source/'eighteenth-eight-followups-independent-review.json').read_text())
        for r in follow['rows']:
            if r['site_id']!=SITE or r['scope'] not in ('scope-1-page-1','scope-1-page-619','scope-1-page-620'):continue
            raw=checked(r['source_file'],r['source_sha256']);rows=c._parse_list(c._make_soup(raw))
            if len(rows)!=r['memberships']:raise ValueError('native boundary membership changed')
            pages[r['scope']]=dict(raw=raw,items=rows,file=r['source_file'],sha256=r['source_sha256'],url=r['url'])
        if [len(pages[k]['items']) for k in ('scope-1','scope-1-page-1','scope-1-page-619','scope-1-page-620')]!=[10,10,9,0]:
            raise ValueError('boundary sample changed')
        if ordering(pages['scope-1']['items'],'signature_date')['ascending_pairs']<1:
            raise ValueError('signature ordering contradiction changed')
        if ordering(pages['scope-1-page-619']['items'],'online_date')['ascending_pairs']<1:
            raise ValueError('online ordering contradiction changed')
        if ordering(pages['scope-1-page-1']['items'],'online_date')['missing']!=1:
            raise ValueError('missing online date changed')
        return pages,sorted(urls)
    finally:c._session.close()


def review(source,probe):
    pages,urls=retained(source);probe=Path(probe);audit=json.loads((probe/'audit.json').read_text())
    if len(audit['requests'])!=1:raise ValueError('unexpected live request count')
    r=audit['requests'][0];raw=(probe/r['body']).read_bytes();cls=CRAWLERS[SITE];c=cls(None,delay=0)
    try:
        current=c._parse_list(c._make_soup(raw));old=pages['scope-1']['items']
        if (r['status']!=200 or r['url']!=pages['scope-1']['url'] or hashlib.sha256(raw).hexdigest()!=r['sha256']
                or len(current)!=10 or [x['external_id'] for x in current]!=[x['external_id'] for x in old]):
            raise ValueError('current first-page identities changed')
        changed=[]
        for before,after in zip(old,current):
            fields={k:dict(before=before[k],after=after[k]) for k in before if before[k]!=after[k]}
            if fields:changed.append(dict(external_id=after['external_id'],fields=fields))
        if ([x['external_id'] for x in changed]!=['BIORISK-2025-MPEX-0123.pdf','BIORISK-2025-MPEX-0122.pdf']
                or any(set(x['fields'])!={'online_date','keywords_raw','keywords','thematics'} for x in changed)
                or any(v['after'] for x in changed for v in x['fields'].values())):
            raise ValueError('current field changes require renewed review')
    finally:c._session.close()
    code=Path(__file__).resolve().parents[2]
    return dict(status='passed',site_id=SITE,reviewed_at=utc_now(),reason=REASON,classification='full_rescan_required',
        original_scope_urls=urls,native_configured_scope=cls.START_URL,original_scopes=3,native_scopes=1,
        sampled_opinion_records=29,all6199_IDs_enumerated=False,actual_PDF_bodies_reviewed_this_batch=0,
        first_signature_order=ordering(pages['scope-1']['items'],'signature_date'),
        second_online_order=ordering(pages['scope-1-page-1']['items'],'online_date'),
        last_online_order=ordering(pages['scope-1-page-619']['items'],'online_date'),
        current_first_page_identity_and_order_match=10,current_first_page_all_fields_match=False,
        current_field_changes=changed,current_online_order=ordering(current,'online_date'),
        current_probe_sha256=hashlib.sha256((probe/'audit.json').read_bytes()).hexdigest(),
        actual_live_HTTP_requests=1,full_scope_equivalence_verified=False,
        source_response_hashes={v['file']:v['sha256'] for v in pages.values()},
        source_sha256=hashlib.sha256(Path(cls.crawl.__code__.co_filename).read_bytes()).hexdigest(),
        dependency_source_sha256={p:hashlib.sha256((code/p).read_bytes()).hexdigest() for p in
                                 (CATALOGUE,'delivery/health/access_challenge.py')},
        baseline_complete=False,global_source_complete=False,all_files_verified=False,operating_database_touched=False)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--probe',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();out=Path(a.out)
    if out.exists():raise FileExistsError(out)
    r=review(a.source,a.probe);atomic_json(out,r);print({'status':r['status'],'sampled_opinion_records':29,'current_identity_match':10,'changed_records':2})
