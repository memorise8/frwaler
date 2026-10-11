"""Revalidate retained delivered access holds without retrying verification gates."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs,urlsplit

from crawler.sites import CRAWLERS
from delivery.health.access_challenge import access_challenge
from delivery.health.results import atomic_json,utc_now

SITE='andra-fr-publications'
REASON='delivered_cookie_verification_hold_and_two_unconfigured_categories_order_end_unverified'
CATALOGUE='docs/deliverables/test_URL_수집대조_20260802.csv'


def retained(source):
    source=Path(source);code=Path(__file__).resolve().parents[2]
    review=json.loads((source/'andra-delivered-access-independent-review.json').read_text())
    audit_path=source/'andra-delivered-entry/audit.json'
    if hashlib.sha256(audit_path.read_bytes()).hexdigest()!=review['source_audit_sha256']:
        raise ValueError('retained access audit changed')
    audit=json.loads(audit_path.read_text())
    with (code/CATALOGUE).open(encoding='utf-8-sig',newline='') as f:
        urls={r['URL주소'] for r in csv.DictReader(f) if r['매칭크롤러(site_id)']==SITE}
    if urls!={r['url'] for r in review['rows']} or len(urls)!=3:
        raise ValueError('delivered route membership changed')
    result={}
    for r in review['rows']:
        raw=(source/r['source_file']).read_bytes()
        a=next(a for a in audit['responses'] if a['url']==r['url'])
        if (hashlib.sha256(raw).hexdigest()!=r['source_sha256'] or a['sha256']!=r['source_sha256']
                or a['response']['status']!=200 or access_challenge(raw)!='javascript_cookie_verification'):
            raise ValueError('retained response no longer proves observed access hold')
        result[r['url']]=dict(raw=raw,file=r['source_file'],sha256=r['source_sha256'],observed_at=a['observed_at'])
    start=CRAWLERS[SITE].crawl.__globals__['_START_URL']
    if start not in urls or parse_qs(urlsplit(start).query).get('f[0]')!=['facet_doc_cat:230']:
        raise ValueError('native category changed; review original scope again')
    return result


def review(source):
    responses=retained(source);code=Path(__file__).resolve().parents[2];cls=CRAWLERS[SITE]
    return dict(status='passed',site_id=SITE,reviewed_at=utc_now(),classification='full_rescan_required',reason=REASON,
        source_sha256=hashlib.sha256(Path(cls.crawl.__code__.co_filename).read_bytes()).hexdigest(),
        dependency_source_sha256={p:hashlib.sha256((code/p).read_bytes()).hexdigest() for p in
                                 ('delivery/health/access_challenge.py',CATALOGUE)},
        retained_delivered_response_count=3,retained_responses={u:{k:v for k,v in r.items() if k!='raw'} for u,r in responses.items()},
        delivered_categories=['230','224','223'],native_configured_category='230',unconfigured_categories=['224','223'],
        source_order=None,source_count=None,source_exhaustion_observed=False,baseline_complete=False,
        actual_live_HTTP_requests_this_batch=0,access_hold_preserved=True,challenge_redirect_requests=0,
        global_source_complete=False,all_files_verified=False,operating_database_touched=False)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();out=Path(a.out)
    if out.exists():raise FileExistsError(out)
    r=review(a.source);atomic_json(out,r);print({'status':r['status'],'retained_responses':3,'live_HTTP':0})
