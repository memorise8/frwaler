"""ARCEP filter mismatch and retained human-verification hold."""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs,urlsplit

from crawler.sites import CRAWLERS
from delivery.health.access_challenge import access_challenge
from delivery.health.results import atomic_json,utc_now

SITE='arcep-fr-actualites'
REASON='annual_filter_human_verification_and_original_scope_query_mismatch'


def retained(source):
    source=Path(source);r=json.loads((source/'arcep-delivered-scope-independent-review.json').read_text())
    c=CRAWLERS[SITE](None,delay=0);pages=[]
    try:
        for row in r['pages']:
            raw=(source/row['source_file']).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=row['source_sha256']:raise ValueError('ARCEP retained page changed')
            items,nxt=c._parse_page(raw.decode())
            if len(items)!=row['native_parsed_rows'] or nxt!=row['native_next_url']:raise ValueError('ARCEP native parser changed')
            pages.append(dict(evidence=row,items=items))
        pubs={x['evidence']['requested_page']:x for x in pages if x['evidence']['scope']=='scope-1'}
        if pubs[32]['items']!=pubs[33]['items'] or len(pubs[32]['items'])!=5:raise ValueError('clamped final page changed')
        original=next(x['evidence']['url'] for x in pages if x['evidence']['scope']=='scope-1')
        old_key='tx_gspublication_publication[filters][type]';active_key='tx_gspublication_publicationlist[filters][type]'
        if parse_qs(urlsplit(original).query).get(old_key)!=['15'] or active_key in parse_qs(urlsplit(original).query):
            raise ValueError('original filter mismatch changed')
        hold=r['access_holds'][0];raw=(source/hold['source_file']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=hold['source_sha256'] or access_challenge(raw)!='javascript_human_verification':
            raise ValueError('retained access hold changed')
        if parse_qs(urlsplit(hold['url']).query).get(active_key)!=['15']:raise ValueError('held filter changed')
        return r,pages,hold,raw
    finally:c._session.close()


def review(source):
    r,pages,hold,raw=retained(source);code=Path(__file__).resolve().parents[2];cls=CRAWLERS[SITE]
    return dict(status='passed',site_id=SITE,reviewed_at=utc_now(),classification='full_rescan_required',reason=REASON,
        source_sha256=hashlib.sha256(Path(cls.crawl.__code__.co_filename).read_bytes()).hexdigest(),
        dependency_source_sha256={'delivery/health/access_challenge.py':hashlib.sha256((code/'delivery/health/access_challenge.py').read_bytes()).hexdigest()},
        native_only_annual_filter=True,original_publication_filter_key_mismatch=True,original_press_scope_unconfigured=True,
        source_boundary_repeats_last_page=True,retained_pages_checked=len(pages),retained_access_classification='javascript_human_verification',
        actual_live_HTTP_requests=0,access_hold_preserved=True,native_browser_launched=False,
        retained_access_url=hold['url'],retained_access_sha256=hold['source_sha256'],
        source_response_hashes={x['evidence']['source_file']:x['evidence']['source_sha256'] for x in pages},
        abstract_is_native_constructed_description_not_PDF_text=True,source_count=None,source_order=None,
        baseline_complete=False,global_source_complete=False,all_files_verified=False,operating_database_touched=False)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();out=Path(a.out)
    if out.exists():raise FileExistsError(out)
    r=review(a.source);atomic_json(out,r);print({'status':r['status'],'live_HTTP':0,'retained_pages':r['retained_pages_checked']})
