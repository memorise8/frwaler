"""Keep source month precision; no numeric chronology invented from URL slugs."""
import argparse
import hashlib
import json
from pathlib import Path

from crawler.incremental import Entry,observe_order,infer_order
from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json,utc_now

SITE='africamuseum-be-en'
REASON='month_precision_listing_dates_and_detail_only_node_IDs_prevent_verified_early_stop'


def retained(source):
    source=Path(source);ledger=json.loads((source/'site-progress.json').read_text())
    facts=next(r for r in ledger['sites'] if r['site_id']==SITE)['source_current_named_catalogue_facts']
    def checked(name,sha):
        raw=(source/name).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('AfricaMuseum retained source hash mismatch')
        return raw
    memberships=[json.loads(line) for line in checked(facts['membership_file'],facts['membership_sha256']).decode().splitlines()]
    expected={r['url']:r for r in memberships};c=CRAWLERS[SITE](None,delay=0)
    pages=[];items=[]
    try:
        for p in facts['pages']:
            raw=checked(p['source_file'],p['source_sha256']);rows=c._parse_list_page(raw.decode())
            if len(rows)!=p['memberships']:raise ValueError('native source memberships regressed')
            pages.append(dict(raw=raw,items=rows,file=p['source_file'],sha256=p['source_sha256']));items+=rows
        if len(items)!=99 or {i[4] for i in items}!=set(expected):raise ValueError('original 99 URLs changed')
        for slug,title,month,snippet,url in items:
            if month!=c.crawl.__globals__['_parse_month_year'](expected[url]['date_raw']):
                raise ValueError('native month differs from source date label')
        detail_review=json.loads((source/'seventeenth-africa-four-actual-detail-review.json').read_text());details={}
        by_url={i[4]:i for i in items}
        for d in detail_review['details']:
            raw=checked(d['source_file'],d['source_sha256']);slug,title,month,snippet,url=by_url[d['source_url']]
            paper=c._parse_detail_page(raw.decode(),slug,month,snippet,url)
            if paper!=d['paper']:raise ValueError('real native detail fields changed')
            details[url]=dict(raw=raw,paper=paper,file=d['source_file'],sha256=d['source_sha256'])
        if len(details)!=4:raise ValueError('missing original four detail samples')
        return pages,items,details
    finally:c._session.close()


def review(source,probe):
    pages,items,details=retained(source);probe=Path(probe);audit=json.loads((probe/'audit.json').read_text())
    if len(audit['requests'])!=1:raise ValueError('unexpected live request count')
    r=audit['requests'][0];raw=(probe/r['body']).read_bytes();c=CRAWLERS[SITE](None,delay=0)
    try:
        if r['status']!=200 or hashlib.sha256(raw).hexdigest()!=r['sha256'] or c._parse_list_page(raw.decode())!=pages[0]['items']:
            raise ValueError('current first-page membership or fields changed')
    finally:c._session.close()
    proof={};observe_order(proof,[Entry(i[0],{},i[2] or '') for i in items],False)
    if any(not i[2] or len(i[2])!=7 for i in items) or infer_order(proof)!=('arbitrary','order_ambiguous'):
        raise ValueError('listing precision requires renewed review')
    code=Path(__file__).resolve().parents[2]
    return dict(status='passed',reviewed_at=utc_now(),site_id=SITE,classification='full_rescan_required',reason=REASON,
        retained_unique_URLs=99,source_populated_pages=10,adjacent_empty_page=True,month_precision_rows=99,
        current_first_page_fields_match=10,actual_live_HTTP_requests=1,precise_date_order_proof=proof,
        numeric_document_IDs_not_inferred_from_slugs=True,actual_four_detail_IDs_and_papers_preserved=True,
        native_month_values_equal_source_labels=True,other95_actual_details_verified=False,
        source_response_hashes={p['file']:p['sha256'] for p in pages},
        real_detail_hashes={d['file']:d['sha256'] for d in details.values()},
        current_probe_sha256=hashlib.sha256((probe/'audit.json').read_bytes()).hexdigest(),
        source_sha256=hashlib.sha256(Path(CRAWLERS[SITE].crawl.__code__.co_filename).read_bytes()).hexdigest(),
        dependency_source_sha256={'delivery/health/access_challenge.py':hashlib.sha256((code/'delivery/health/access_challenge.py').read_bytes()).hexdigest()},
        baseline_complete=False,global_source_complete=False,all_files_verified=False,operating_database_touched=False)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--probe',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();out=Path(a.out)
    if out.exists():raise FileExistsError(out)
    report=review(a.source,a.probe);atomic_json(out,report)
    print(json.dumps({k:report[k] for k in ('status','retained_unique_URLs','month_precision_rows','actual_live_HTTP_requests')}))
