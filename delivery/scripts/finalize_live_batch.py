"""Audit bounded live-check outcomes, including explicitly allowed inability.

Completion here means every requested site has evidence and a justified final
disposition. It does NOT mean every collector works, every strategy is proven,
or all documents were counted/collected. No network or database operations.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qsl,urlsplit,urlunsplit,urlencode

from delivery.health.results import atomic_json,utc_now
from delivery.scripts.audit_live_structure import audit_site,normalized
from delivery.scripts.live_check_contracts import CHECKS,family
from delivery.scripts.live_count_estimates import SOURCE,ARITHMETIC,PARTITIONS,SITEMAP,UNKNOWN
from delivery.scripts.live_check_structure import summarize


def wp_scope(url):
    p=urlsplit(url)
    if '/wp-json/wp/v2/' not in p.path:return None
    controls={'page','per_page','order','orderby','_fields'}
    return urlunsplit(p._replace(query=urlencode(sorted((k,v) for k,v in parse_qsl(p.query,keep_blank_values=True) if k not in controls))))


def evidence_reasons(result):
    reasons=[]
    for scope in result['scopes']:
        if scope.get('reason'):reasons.append(scope['reason'])
        reasons.extend(scope.get('limitations',[]))
        for check in scope['checks'].values():
            reason=check.get('reason')
            if reason and reason not in ('not_observed','not_yet_observed'):reasons.append(reason)
    for response in result['requests']:
        if response.get('error'):reasons.append('request_'+str(response['number'])+':'+response['error'])
        summary=response.get('summary',{})
        if summary.get('javascript_shell'):reasons.append('request_'+str(response['number'])+':JS_shell_requires_collection_specific_contract')
        if summary.get('format')=='html' and not summary.get('item_count'):
            reasons.append('request_'+str(response['number'])+':no_listing_IDs_extracted_from_observed_HTML')
        reasons.extend('request_'+str(response['number'])+':'+x for x in summary.get('anomalies',[]))
    closure=result.get('URL_closure',{})
    if closure.get('stopped_reason'):reasons.append(closure['stopped_reason'])
    if result.get('inherited_access_evidence'):reasons.append('preserved_observed_source_access_hold')
    return list(dict.fromkeys(reasons))


def disposition(plan,result):
    base=audit_site(plan,result);issues=list(base['integrity_issues']);requests=result['requests']
    observed={normalized(r['url']):r for r in requests}
    closure=result.get('URL_closure',{});grounds=evidence_reasons(result)
    source_stop=bool(result['status']=='access_blocked' or result.get('inherited_access_evidence') or closure.get('hold_evidence')
                     or any(o.get('status')=='AccessStopped' for o in closure.get('observations',[])))
    if result['status']!='confirmed' and not grounds:issues.append('no_specific_inability_ground')
    if result['status']=='confirmed' and result.get('strategy_review',{}).get('status')!='reviewed_requested_native_collection':
        issues.append('confirmed_strategy_without_semantic_review')
    urls=[]
    for group in plan['targets']:
        for target in group:
            url=target['url'];entry=dict(**target)
            response=observed.get(normalized(url))
            if response:
                entry.update(disposition='observed_response' if response.get('status') is not None else 'transport_unavailable',
                             response=response['number'],status=response.get('status'),error=response.get('error'))
            elif source_stop:entry.update(disposition='not_requested_after_source_or_robots_stop',grounds=grounds)
            elif len(requests)>=result['request_budget']:entry.update(disposition='bounded_request_budget_exhausted',requests_used=len(requests))
            elif wp_scope(url) and (matches:=[r['number'] for r in requests if wp_scope(r['url'])==wp_scope(url)]):
                entry.update(disposition='native_metadata_projection_or_boundary_sample',responses=matches,
                             limitation='Exact literal URL may differ in projection/page size/order; unsupported content or scope reconciliation remains unconfirmed.')
            elif target['provenance']=='original_registered_scope' and (matches:=[r['number'] for r in requests if family(r['url'])==family(url)]):
                entry.update(disposition='registered_collection_page_variant_observed',responses=matches)
            elif target['provenance']=='original_registered_scope' and result['status']=='unconfirmable_custom' and grounds:
                entry.update(disposition='supplementary_entry_not_resolved_by_common_contract',grounds=grounds)
            elif '/.../' in url or '/…/' in url:
                entry.update(disposition='nonconcrete_path_requires_source_specific_binding')
            elif any(o.get('url')==url and o.get('status') in ('ValueError','BudgetReached') for o in closure.get('observations',[])):
                entry.update(disposition='unavailable_under_recorded_request_contract',evidence=[o for o in closure['observations'] if o.get('url')==url])
            else:
                entry.update(disposition='unresolved_actionable_URL');issues.append('unresolved_actionable_URL:'+url)
            urls.append(entry)
    count_fields=('expected_items','count_basis','count_confidence','hidden_cap_suspected')
    if any(k not in result for k in count_fields):issues.append('required_count_fields_missing')
    else:
        if result['expected_items'] is not None and (type(result['expected_items']) is not int or result['expected_items']<0):issues.append('invalid_expected_items')
        if result['count_basis'] not in (SOURCE,ARITHMETIC,PARTITIONS,SITEMAP,UNKNOWN):issues.append('invalid_count_basis')
        if result['count_confidence'] not in ('높음','보통','낮음') or type(result['hidden_cap_suspected']) is not bool:issues.append('invalid_count_qualifiers')
    verdict='strategy_confirmed' if result['status']=='confirmed' else 'access_or_policy_unavailable' if source_stop else 'custom_verification_unavailable'
    clauses=[x.strip() for x in re.split(r';|(?<!\d)\s+(?=\(?\d+\))',plan['live_check']) if x.strip()]
    return dict(site_id=plan['site_id'],delivery=plan['delivery'],verdict=verdict,issues=sorted(set(issues)),
        live_check=plan['live_check'],URL_dispositions=urls,unresolved_reference_dispositions=[dict(**u,disposition='custom_binding_or_budget_required') for u in plan['unresolved_URLs']],
        requirements=list(CHECKS),scope_checks=[dict(url=s['url'],status=s['status'],checks=s['checks']) for s in result['scopes']],
        condition_dispositions=[dict(condition=c,status='reviewed_native_metadata_conditions' if verdict=='strategy_confirmed' else 'unconfirmed_under_allowed_inability_disposition',
            grounds=grounds if verdict!='strategy_confirmed' else [result.get('strategy_review',{}).get('scope_basis','semantic_review_missing')],
            limitation='Unconfirmed conditions are not represented as passed.') for c in clauses],
        strategy=result['strategy'],confidence=result['confidence'],counts={k:result.get(k) for k in count_fields})


def finalize(base,batch='primary',write_completion=False):
    out=base/('live-check-20261012' if batch=='primary' else 'live-check-extra-20261012')
    plans=json.loads((out/'plans.json').read_text());summary=summarize(out,base,plans)
    reviewed=[];missing=[]
    for plan in plans['sites']:
        path=out/'sites'/(plan['site_id']+'.json')
        if not path.exists():missing.append(plan['site_id']);continue
        result=json.loads(path.read_text());item=disposition(plan,result)
        item['result_file']=str(path);item['result_sha256']=hashlib.sha256(path.read_bytes()).hexdigest();reviewed.append(item)
    artifact=dict(reviewed_at=utc_now(),batch=batch,total=plans['registered'],processed=len(reviewed),
        missing=missing,issues={r['site_id']:r['issues'] for r in reviewed if r['issues']},
        counts=dict(Counter(r['verdict'] for r in reviewed)),sites=reviewed,
        completion_kind='bounded_live_check_with_explicit_unavailable_dispositions',
        full_collection_verification=False,documents_saved=0,database_connections=0,
        queue_sha256=plans['queue_sha256'],plans_sha256=hashlib.sha256((out/'plans.json').read_bytes()).hexdigest())
    atomic_json(out/'disposition-audit.json',artifact)
    complete=not missing and not artifact['issues'] and len(reviewed)==plans['registered']
    if write_completion:
        if not complete:raise ValueError('unresolved requirements remain; completion marker not written')
        marker={k:v for k,v in artifact.items() if k not in ('sites','missing','issues')}
        marker.update(complete=True,disposition_audit=str(out/'disposition-audit.json'),
                      disposition_audit_sha256=hashlib.sha256((out/'disposition-audit.json').read_bytes()).hexdigest())
        atomic_json(out/(batch+'-completion.json'),marker)
        progress=json.loads((base/'progress.json').read_text());key='live_structure_check' if batch=='primary' else 'live_structure_check_extra'
        progress[key].update(batch_completed=True,completion_evidence=str(out/(batch+'-completion.json')))
        atomic_json(base/'progress.json',progress)
    return dict(total=plans['registered'],processed=len(reviewed),issue_sites=len(artifact['issues']),counts=artifact['counts'],audit_passed=complete,completion_written=bool(write_completion and complete))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base',required=True,type=Path);p.add_argument('--batch',choices=('primary','extra'),default='primary');p.add_argument('--write-completion',action='store_true')
    a=p.parse_args();print(json.dumps(finalize(a.base,a.batch,a.write_completion),ensure_ascii=False))
