"""Bounded ACCC access evidence: no source count/order inferred from denial."""
import argparse
import hashlib
import json
from pathlib import Path

from crawler.sites import CRAWLERS
from delivery.health.access_challenge import access_challenge
from delivery.health.results import atomic_json, utc_now

SITES={'accc-gov-au-about-us':'https://www.accc.gov.au/about-us/publications',
       'accc-gov-au-search':'https://www.accc.gov.au/search?query=pdf&page=0'}
REASON='observed_http403_edge_access_denied_source_identity_order_and_exhaustion_unverified'
DEPENDENCY='delivery/health/access_challenge.py'


def captures(root):
    root=Path(root);result={}
    for name in ('probe','native-search-entry'):
        audit=json.loads((root/name/'audit.json').read_text())
        for r in audit['requests']:
            raw=(root/name/r['body']).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=r['sha256']:raise ValueError('captured response hash mismatch')
            if r['status']!=403 or access_challenge(raw)!='akamai_access_denied':
                raise ValueError('ACCC response does not match observed access hold')
            result[r['url']]={'body':raw,'status':r['status'],'sha256':r['sha256'],
                              'file':str(Path(name)/r['body'])}
    if set(result)!=set(SITES.values())|{'https://www.accc.gov.au/search?query=pdf'}:
        raise ValueError('missing exact native ACCC entry response')
    return result


def run(root,out):
    root,out=Path(root),Path(out)
    if out.exists():raise FileExistsError(out)
    responses=captures(root);code=Path(__file__).resolve().parents[2]
    dependency={DEPENDENCY:hashlib.sha256((code/DEPENDENCY).read_bytes()).hexdigest()}
    sites=[]
    for site,url in SITES.items():
        cls=CRAWLERS[site]
        sites.append(dict(site_id=site,classification='full_rescan_required',reason=REASON,
            exact_native_entry=url,response_status=403,response_sha256=responses[url]['sha256'],
            response_file=responses[url]['file'],access_classification='akamai_access_denied',
            source_sha256=hashlib.sha256(Path(cls.crawl.__code__.co_filename).read_bytes()).hexdigest(),
            dependency_source_sha256=dependency,source_count=None,source_order=None,baseline_complete=False,
            current_source_scope_verified=False))
    report=dict(status='passed',reviewed_at=utc_now(),sites=sites,actual_live_HTTP_requests=3,
        challenge_bypass_attempted=False,automatic_retries=0,source_exhaustion_observed=False,
        denial_reference_numbers_not_exported=True,global_source_complete=False,all_files_verified=False,
        operating_database_touched=False,source_documents_deleted=False,
        note='The delivered URLs and exact native first-page URL returned HTTP403 on the audited measurement path. This does not prove missing content, source exhaustion, permanent unavailability, or a source total. Native crawlers now fail explicitly on the observed access body instead of accepting an empty list.')
    atomic_json(out,report);print(json.dumps({'status':report['status'],'sites':len(sites),'actual_live_HTTP_requests':3}))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();run(a.root,a.out)
