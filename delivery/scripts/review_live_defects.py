"""Offline source/measurement reconciliation for collector defect candidates."""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from delivery.health.results import atomic_json, utc_now
from delivery.scripts.live_check_contracts import PAGE_KEYS

ROOT=Path(__file__).resolve().parents[2]


def scope_key(url,params=None):
    p=urlsplit(url);query=dict(parse_qsl(p.query,keep_blank_values=True));query.update(params or {})
    query={k:str(v) for k,v in query.items() if k.lower() not in PAGE_KEYS and v not in ('',None)}
    return urlunsplit(p._replace(query=urlencode(sorted(query.items())),fragment=''))


def reference(path,line=None):
    return dict(path=str(path),line=line,sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def review(out):
    results={p.stem:json.loads(p.read_text()) for p in (out/'sites').glob('*.json')}
    findings=[];rejected=[];generic=ROOT/'crawler/generic_crawler.py'
    source=generic.read_text()
    if 'query_pagination = pagination.get("type") == "query_param"' not in source or 'if not query_pagination:' not in source:
        raise ValueError('generic pagination implementation changed; review source again')
    for site,result in results.items():
        config_path=ROOT/'crawler/sites/configs'/(site+'.json')
        if Path(result['source_file'])!=generic or not config_path.exists():continue
        config=json.loads(config_path.read_text());listing=config.get('list_page',{});pagination=listing.get('pagination',{})
        if pagination.get('type')!='none':continue
        for scope in result['scopes']:
            check=scope['checks']['page_parameter']
            if check.get('status')!='verified_by_distinct_IDs':continue
            if scope_key(listing['url'],listing.get('params'))!=scope_key(scope['url']):continue
            findings.append(dict(site_id=site,kind='pagination_disabled_despite_distinct_source_pages',
                status='confirmed_configuration_defect',scope=scope['url'],
                evidence=check,source_references=[reference(config_path),reference(generic,155),reference(generic,232)],
                effect='Generic collector sends no page parameter and exits after the first list iteration; source exposes distinct later records.',
                limitation='No crawler or database was executed; other parser/detail defects may coexist.'))
    # Manually reviewed native REST routes. Compare item capacity, never page
    # numbers from the two-item reconnaissance projection with collector pages.
    for site,page_size,required_query,line in (
        ('ukdataservice-ac-uk-2026',100,{},18),
        ('ypergasias-gov-gr-category',25,{'categories':'4'},41),
    ):
        result=results.get(site)
        if not result:continue
        source_path=Path(result['source_file']);text=source_path.read_text()
        expected='_PER_PAGE = 100' if page_size==100 else 'PAGE_SIZE = 25'
        if expected not in text:raise ValueError('reviewed native REST page size changed: '+site)
        for scope in result['scopes']:
            if not urlsplit(scope['url']).path.endswith('/wp-json/wp/v2/posts'):continue
            query=dict(parse_qsl(urlsplit(scope['url']).query))
            if any(query.get(k)!=v for k,v in required_query.items()):continue
            check=scope['checks']['total_arithmetic']
            if check.get('status')!='verified':continue
            total=check['total'];needed=math.ceil(total/page_size)
            if needed>200:continue
            rejected.append(dict(site_id=site,kind='collector_cap_below_observed_last_page',status='rejected_current_truncation_claim',
                observed_total=total,reconnaissance_page_size=check['page_size'],collector_page_size=page_size,
                collector_pages_needed=needed,collector_default_page_cap=200,
                reason='Normalized collector page count fits the cap. Direct comparison to reconnaissance page count is invalid.',
                source_reference=reference(source_path,line),response_evidence=scope['checks']['last_and_after'],
                limitation='Does not prove overall collection completion or remove time-budget/scope risks.'))
            break
    pair=['www2-mrc-lmb-cam-ac-uk','www2-mrc-lmb-cam-ac-uk-research']
    if all(site in results for site in pair):
        old='https://www2.mrc-lmb.cam.ac.uk/research/published-research/'
        evidence=[]
        for site in pair:
            for scope in results[site]['scopes']:
                migration=scope['checks']['domain_migration']
                if scope['url']==old and migration.get('resolved_url')=='https://mrclmb.ac.uk/publications/' and migration.get('old_domain_redirect_observed'):
                    evidence.append(dict(site_id=site,migration=migration,source_reference=reference(Path(results[site]['source_file']))))
        if len(evidence)==2:
            findings.append(dict(site_ids=pair,kind='duplicate_registration_after_domain_migration',
                status='confirmed_registration_overlap',original_URL=old,resolved_URL='https://mrclmb.ac.uk/publications/',evidence=evidence,
                limitation='Same registered source URL, not a claim of duplicate database rows or equivalent crawler output.'))
    artifact=dict(updated_at=utc_now(),confirmed=findings,rejected_candidates=rejected,
        confirmed_counts=dict(Counter(r['kind'] for r in findings)),
        remaining_candidates=str(out/'collector-defects.json'),documents_saved=0,database_connections=0)
    atomic_json(out/'collector-defects-reviewed.json',artifact)
    return {k:v for k,v in artifact.items() if k not in ('confirmed','rejected_candidates')}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--directory',required=True,type=Path)
    args=parser.parse_args();print(json.dumps(review(args.directory),ensure_ascii=False))


if __name__=='__main__':main()
