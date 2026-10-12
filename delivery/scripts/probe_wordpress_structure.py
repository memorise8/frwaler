"""Metadata-only WordPress boundary cross-check for a reviewed native endpoint.

GET six small JSON listings. No content/title/abstract/PDF fields are requested
or retained. Earlier first-pass requests count against the per-site ceiling.
"""
import argparse
from datetime import datetime, timedelta
import copy
import hashlib
import json
from pathlib import Path
import socket
from urllib.parse import urlencode,urlsplit,parse_qs

from delivery.health.results import atomic_json,utc_now
from delivery.scripts.discover_collection_structure import ReconTransport
from delivery.scripts.discovery_structure import finalize,blank
from delivery.scripts.route_structure_discovery import security_hold

FIELDS='id,date,date_gmt,modified,link'


def parse_metadata(raw,response,endpoint,per_page=2):
    data=json.loads(raw)
    if not isinstance(data,list):raise ValueError('WordPress list response required')
    h={k.lower():v for k,v in response.get('source_headers',{}).items()}
    total,pages=int(h['x-wp-total']),int(h['x-wp-totalpages'])
    if total<0 or pages!=(total+per_page-1)//per_page:raise ValueError('inconsistent WordPress count headers')
    if len(data)>per_page:raise ValueError('WordPress page size ignored')
    items=[]
    for item in data:
        if not isinstance(item,dict) or type(item.get('id')) is not int or item['id']<=0:raise ValueError('invalid WordPress identity')
        if set(item)-set(FIELDS.split(',')):raise ValueError('WordPress field projection ignored')
        stamp=datetime.fromisoformat(item['date']);link=item['link']
        if urlsplit(link).netloc!=urlsplit(endpoint).netloc:raise ValueError('item URL left native site')
        items.append(dict(id=str(item['id']),date=stamp.date().isoformat(),source_datetime=item['date'],
                          date_basis='WordPress_CMS_date_not_research_publication_date',url=link,identity_basis='WordPress_id'))
    if len({i['id'] for i in items})!=len(items):raise ValueError('repeated WordPress identities')
    summary=blank();summary.update(format='json',item_identity_method='WordPress_id',
        totals=[dict(value=total,source='X-WP-Total',role='native_publications_scope_total')],total_pages=pages)
    return finalize(summary,items),total,pages,items


def verify(observations):
    desc,asc,last,beyond,before,repeat=observations
    if not desc['items'] or len(asc['items'])!=2:raise ValueError('insufficient nonempty boundary samples')
    totals=[x['total'] for x in (desc,asc,last,repeat)]
    if len(set(totals))!=1:raise ValueError('source total changed')
    if len(last['items'])!=totals[0]-(last['pages']-1)*2:raise ValueError('last page length contradicts total')
    if [x['source_datetime'] for x in asc['items']]!=sorted(x['source_datetime'] for x in asc['items']):raise ValueError('ascending sort not observed')
    if [x['source_datetime'] for x in desc['items']]!=sorted((x['source_datetime'] for x in desc['items']),reverse=True):raise ValueError('descending sort not observed')
    # Same timestamp can have several IDs. Require overlap and explicitly keep
    # the tie limitation; do not invent a unique oldest record.
    oldest=asc['items'][0]
    if min(x['source_datetime'] for x in last['items'])!=oldest['source_datetime']:raise ValueError('ascending head and descending last disagree')
    if oldest['id'] not in {x['id'] for x in last['items']}:raise ValueError('oldest ID boundary not cross-confirmed')
    if beyond.get('code')!='rest_post_invalid_page_number' or beyond.get('status')!=400:raise ValueError('adjacent page did not explicitly reject invalid page')
    if before['items'] or before['total']!=0:raise ValueError('before-oldest filter not confirmed empty')
    if asc['items']!=repeat['items']:raise ValueError('ascending IDs or dates changed on repeat')
    return dict(oldest=oldest,total=totals[0],last_page=last['pages'],ascending_and_descending_boundary_match=True,
                before_oldest_filter_empty=True,invalid_page_after_reported_end=True,
                oldest_unique_across_timestamp_ties_not_proven=True)


def validate_report(report):
    if report['status']!='passed' or len(report['requests'])!=6:raise ValueError('complete six-request evidence required')
    facts=verify(report['observations']);endpoint=urlsplit(report['endpoint'])
    before=(datetime.fromisoformat(facts['oldest']['source_datetime'])-timedelta(seconds=1)).isoformat()
    for index,response in enumerate(report['requests']):
        url=urlsplit(response['url'])
        if (url.scheme,url.netloc,url.path)!=(endpoint.scheme,endpoint.netloc,endpoint.path):raise ValueError('evidence endpoint scope mismatch')
        expected=dict(per_page=['2'],page=[str(facts['last_page'] if index==2 else facts['last_page']+1 if index==3 else 1)],
                      orderby=['date'],order=['asc' if index in (1,5) else 'desc'],_fields=[FIELDS])
        if index==4:expected['before']=[before]
        if parse_qs(url.query)!=expected:raise ValueError('evidence query changed native scope')
        if response['status']!=(400 if index==3 else 200) or not response.get('sha256'):raise ValueError('response status/hash missing')
        if index!=3:
            observed=report['observations'][index]
            if response['summary']['items']!=observed['items'] or response['summary']['totals'][0]['value']!=observed['total']:
                raise ValueError('measurement summary disagrees with verification')
    return facts


def apply_existing(out,site):
    out=Path(out);directory=out/'followups'/site/'wordpress-v1';report_path=directory/'audit.json'
    report=json.loads(report_path.read_text());facts=validate_report(report);path=out/'sites'/(site+'.json')
    original_raw=path.read_bytes();original=json.loads(original_raw)
    if hashlib.sha256(original_raw).hexdigest()!=report['original_result_sha256']:
        raise ValueError('first-pass record changed; review merge required')
    if len(original['scopes'])!=1 or urlsplit(original['scopes'][0]['url']).path not in ('','/'):
        raise ValueError('automatic merge limited to a single landing-page entry')
    previous=directory/'first-pass-result.json'
    if previous.exists():raise FileExistsError(previous)
    previous.write_bytes(original_raw)
    result=copy.deepcopy(original);result['requests']+=report['requests'];requests=report['requests']
    response=lambda n:requests[n]['number']
    features=dict(
        oldest_sort=dict(status='measured',verified=True,method='GET orderby=date&order=asc',response=response(1),
                         date_role='WordPress CMS datetime, not research publication date'),
        last_page=dict(status='measured',verified=True,url=requests[2]['url'],response=response(2),
                       page_number=facts['last_page'],page_size=2,end_evidence='X-WP-TotalPages and explicit adjacent invalid-page code'),
        date_filters=dict(status='measured',tested=True,controls=[dict(name='before',method='GET')],response=response(4),
                          before_oldest_returns_zero=True,year_partition_completeness_not_tested=True),
        limits_and_wraparound=dict(status='measured',beyond_response=response(3),wraparound_or_clamp=False,
                                  invalid_page_code='rest_post_invalid_page_number',no_cap_observed_through_page=facts['last_page'],
                                  hidden_cap_absence_proven=False),
        oldest_item=dict(status='cross_confirmed_native_collection_boundary',item=facts['oldest'],
                         scope_oldest_proven=True,global_oldest_proven=False,oldest_tie_uniqueness_unproven=True),
        independent_evidence=dict(status='measured',reported_counts=[dict(value=facts['total'],source='X-WP-Total')],
                                  scope_equivalence_verified=True,methods=['ascending first versus descending last','before-oldest empty result'],
                                  source_count_is_current_native_collection_not_entire_website=True),
        identity_stability=dict(status='measured',method='WordPress id and item link',same_ordered_IDs_on_repeat=True,
                                response=response(5),long_term_stability_verified=False),
        measurement_origin=dict(status='observed',server=report['server'],at=report['started_at'],finished_at=report['finished_at'],
                                endpoint=report['endpoint']))
    scope=result['scopes'][0];scope.update(features=features,status='measured',strategy='오래된 순 정렬',confidence='높음',
        first_response=response(0),blocked_reasons=[],collection_url=report['endpoint'],
        source_relation='Registered native collector public publications endpoint; original root URL is a landing entry, not a global website catalogue',
        cross_checks=[dict(method='ascending_first_vs_descending_last',responses=[response(1),response(2)],conclusive=True),
                      dict(method='before_oldest_date_filter',response=response(4),conclusive=True)])
    result.update(status='measured',measured_tier='높음',strategy=['오래된 순 정렬'],confidence='높음',finished_at=report['finished_at'],
        actual_HTTP_requests=original['actual_HTTP_requests']+6,request_budget=30,
        followup_evidence=str(report_path.relative_to(out)),followup_evidence_sha256=hashlib.sha256(report_path.read_bytes()).hexdigest(),
        first_pass_evidence=str(previous.relative_to(out)),registered_collection_scope_confirmed=True)
    atomic_json(path,result)
    atomic_json(directory/'acceptance.json',dict(status='passed',accepted_at=utc_now(),facts=facts,
        final_result_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),original_result_preserved=True,
        operating_database_touched=False,documents_saved=0))
    print(dict(site_id=site,status=result['status'],measured_tier=result['measured_tier'],total_site_requests=len(result['requests'])))


def run(out,site,endpoint):
    out=Path(out);original_path=out/'sites'/(site+'.json');original=json.loads(original_path.read_text())
    hints=json.loads((out/'source-hints.json').read_text())['sites'][site]
    if endpoint not in {x['url'] for x in hints['endpoints']} or '/wp-json/wp/v2/' not in endpoint:
        raise ValueError('reviewed native WordPress endpoint hint required')
    held=json.loads((out/'access-holds.json').read_text())
    if security_hold(original,held):raise ValueError('observed access hold prohibits alternate routes')
    manifest=json.loads((out/'manifest.json').read_text());host=urlsplit(endpoint).hostname
    peers=[r for r in manifest['sites'] if any(urlsplit(s['url']).hostname==host for s in r['scopes'])]
    if any(not (out/'sites'/(r['site_id']+'.json')).exists() for r in peers):raise ValueError('same-host first-pass sites still pending')
    prior=len(original.get('requests',[]));attempt_dir=out/'followups'/site/'wordpress-v1';attempt_dir.mkdir(parents=True,exist_ok=False)
    if prior+6>30:raise ValueError('insufficient site request budget')
    transport=ReconTransport(out,out.parent);requests=[];observations=[]
    report=dict(status='running',site_id=site,endpoint=endpoint,source_hint=hints,server=socket.gethostname(),started_at=utc_now(),
                prior_request_count=prior,requests=requests,documents_saved=0,database_connections=0,
                original_result_sha256=hashlib.sha256(original_path.read_bytes()).hexdigest())
    def persist():atomic_json(attempt_dir/'audit.json',report)
    def fetch(purpose,*,order='desc',page=1,before=None,error=False):
        if len(requests)>=6 or (out/'STOP').exists():raise ValueError('request budget or stop condition')
        query=dict(per_page=2,page=page,orderby='date',order=order,_fields=FIELDS)
        if before:query['before']=before
        url=endpoint+'?'+urlencode(query);response,raw=transport.fetch(url,inspect_error_json=error)
        response.update(purpose=purpose,number=prior+len(requests)+1);requests.append(response);persist()
        if raw is None:raise ValueError('source response unavailable: '+str(response.get('error')))
        if error:
            data=json.loads(raw);value=dict(code=data.get('code'),status=response['status'],response=response['number'])
            response['error_code_summary']=value;persist();return value
        summary,total,pages,items=parse_metadata(raw,response,endpoint);response['summary']=summary;persist()
        return dict(items=items,total=total,pages=pages,response=response['number'])
    persist()
    try:
        desc=fetch('native_newest_metadata');asc=fetch('native_oldest_metadata',order='asc')
        last=fetch('reported_last_page_metadata',page=desc['pages'])
        beyond=fetch('one_page_after_reported_total',page=desc['pages']+1,error=True)
        threshold=(datetime.fromisoformat(asc['items'][0]['source_datetime'])-timedelta(seconds=1)).isoformat()
        before=fetch('date_filter_before_oldest',before=threshold)
        repeat=fetch('repeat_oldest_metadata',order='asc')
        observations=[desc,asc,last,beyond,before,repeat];facts=verify(observations)
        report.update(status='passed',facts=facts,observations=observations,finished_at=utc_now(),
                      total_site_requests=prior+len(requests),scope='registered_native_publications_collection',
                      homepage_not_a_global_website_catalogue=True)
    except Exception as exc:
        report.update(status='needs_review',error_type=type(exc).__name__,reason=str(exc),observations=observations,
                      finished_at=utc_now(),total_site_requests=prior+len(requests));raise
    finally:persist()
    print(json.dumps({k:report[k] for k in ('status','site_id','facts','total_site_requests')},ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);p.add_argument('--site',required=True);p.add_argument('--endpoint');p.add_argument('--apply-existing',action='store_true')
    a=p.parse_args()
    if a.apply_existing:apply_existing(a.out,a.site)
    elif a.endpoint:run(a.out,a.site,a.endpoint)
    else:p.error('--endpoint required for a live probe')
