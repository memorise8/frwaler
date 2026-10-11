"""AMI year groups do not certify within-year chronology for early stopping."""
import argparse
import hashlib
import json
from pathlib import Path
import re

from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json,utc_now

SITE='ami-swiss-en'
REASON='year_only_source_groups_do_not_define_within_year_incremental_order'


def retained(source):
    source=Path(source);ledger=json.loads((source/'site-progress.json').read_text())
    facts=next(r for r in ledger['sites'] if r['site_id']==SITE)['source_current_named_catalogue_facts']
    def checked(name,sha):
        raw=(source/name).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('AMI retained source changed')
        return raw
    records=[json.loads(s) for s in checked(facts['membership_file'],facts['membership_sha256']).decode().splitlines()]
    audit=json.loads((source/'ami-swiss-en-exact-delivered-entries/audit.json').read_text());r=audit['responses'][-1]
    landing=checked('ami-swiss-en-exact-delivered-entries/'+r['file'],r['sha256'])
    years=sorted(set(re.findall(r'name="year" value="(\d{4})"',landing.decode())),reverse=True)
    if years!=[str(v) for v in facts['years']]:raise ValueError('original year menu changed')
    c=CRAWLERS[SITE](None,delay=0);pages=[];items=[]
    try:
        for p in facts['pages']:
            year=p['scope'].split('-')[-1];raw=checked(p['source_file'],p['source_sha256']);rows=c._parse_year_page(raw.decode(),year)
            expected={r['doi']:r for r in records if r['year']==year}
            if {r['doi'] for r in rows}!=set(expected) or any(r['year']!=year or r['url']!=expected[r['doi']]['url'] for r in rows):
                raise ValueError('native year DOI/URL membership changed')
            pages.append(dict(year=year,raw=raw,file=p['source_file'],sha256=p['source_sha256']));items+=rows
        if len(records)!=1382 or len(items)!=1372 or len({r['doi'] for r in items})!=1370 or len({r['url'] for r in items})!=1372:
            raise ValueError('original year memberships changed')
        prior=json.loads((source/'seventeenth-ami-dates-committed-review.json').read_text());crossref={}
        for r in prior['actual_Crossref_responses']:
            raw=checked(r['crossref_file'],r['crossref_sha256']);msg=json.loads(raw)['message'];doi=r['source']['doi']
            if msg['DOI'].casefold()!=doi.casefold() or c._crossref_date(msg)!=r['crossref_date_after']:
                raise ValueError('actual DOI or native date semantics changed')
            abstract=c.crawl.__globals__['_strip_jats'](msg.get('abstract') or '')
            if bool(len(abstract)>=c._MIN_ABSTRACT)!=bool(r['after_count']):raise ValueError('actual abstract inclusion changed')
            crossref[doi]=dict(raw=raw,message=msg,prior=r)
        if len(crossref)!=3:raise ValueError('missing Crossref source samples')
        return landing,pages,items,crossref
    finally:c._session.close()


def review(source,probe):
    landing,pages,items,crossref=retained(source);probe=Path(probe);audit=json.loads((probe/'audit.json').read_text())
    if len(audit['requests'])!=1:raise ValueError('unexpected live request count')
    r=audit['requests'][0];body=(probe/r['body']).read_bytes();cls=CRAWLERS[SITE]
    if r['url']!=cls._LIST_URL or r['status']!=500 or body or hashlib.sha256(body).hexdigest()!=r['sha256']:
        raise ValueError('current source access requires renewed review')
    code=Path(__file__).resolve().parents[2]
    return dict(status='passed',reviewed_at=utc_now(),site_id=SITE,classification='full_rescan_required',reason=REASON,
        retained_years=19,retained_source_memberships=1382,native_per_year_unique_memberships=1372,
        distinct_original_URLs=1372,distinct_DOIs=1370,same_DOI_different_year_URL_variants=2,
        source_date_precision='year',within_year_order_verified=False,actual_live_HTTP_requests=1,current_entry_status=500,
        current_source_count=None,actual_Crossref_responses=3,actual_abstract_eligible_samples=2,actual_abstract_exclusions=1,
        all1370_actual_enrichments_verified=False,source_response_hashes={p['file']:p['sha256'] for p in pages},
        actual_crossref_hashes={r['prior']['crossref_file']:r['prior']['crossref_sha256'] for r in crossref.values()},
        current_probe_sha256=hashlib.sha256((probe/'audit.json').read_bytes()).hexdigest(),
        source_sha256=hashlib.sha256(Path(cls.crawl.__code__.co_filename).read_bytes()).hexdigest(),
        dependency_source_sha256={'delivery/health/access_challenge.py':hashlib.sha256((code/'delivery/health/access_challenge.py').read_bytes()).hexdigest()},
        baseline_complete=False,global_source_complete=False,all_files_verified=False,operating_database_touched=False)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--probe',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();out=Path(a.out)
    if out.exists():raise FileExistsError(out)
    r=review(a.source,a.probe);atomic_json(out,r);print({k:r[k] for k in ('status','distinct_DOIs','current_entry_status')})
