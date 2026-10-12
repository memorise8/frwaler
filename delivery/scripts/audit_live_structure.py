"""Offline completeness audit; never upgrades probe uncertainty into proof.

Run with --directory <base>/live-check-20261012. No network or database access.
The evidence ledger retains every requested URL and all nine required checks.
An automatic pass is an artifact integrity check, not semantic sign-off.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from delivery.health.results import atomic_json, utc_now
from delivery.scripts.live_check_contracts import CHECKS


def normalized(url):
    p=urlsplit(url)
    return urlunsplit(p._replace(query=urlencode(sorted(parse_qsl(p.query,keep_blank_values=True))),fragment=''))


def audit_site(plan,result):
    issues=[];requests=result.get('requests',[]);scopes=result.get('scopes',[])
    if result.get('site_id')!=plan['site_id']:issues.append('identity_mismatch')
    if result.get('live_check')!=plan['live_check']:issues.append('requested_conditions_changed')
    if result.get('status') in ('running','runner_error'):issues.append('checker_not_finished_successfully')
    if len(requests)>30 or len(requests)>result.get('request_budget',30):issues.append('request_budget_exceeded')
    if result.get('database_connections')!=0 or result.get('documents_saved')!=0:issues.append('non_metadata_side_effect_reported')
    if result.get('actual_HTTP_responses')!=sum(r.get('status') is not None for r in requests):issues.append('response_count_mismatch')
    if [r.get('number') for r in requests]!=list(range(1,len(requests)+1)):issues.append('request_sequence_not_contiguous')
    inherited=result.get('inherited_access_evidence',{})
    if not scopes and not inherited:issues.append('no_live_or_inherited_evidence')
    for scope in scopes:
        if set(scope.get('checks',{}))!=set(CHECKS):issues.append('required_check_missing:'+scope['url'])
        if scope.get('status')!='confirmed' and not (scope.get('reason') or scope.get('limitations') or
                any(c.get('reason') and c['reason']!='not_observed' for c in scope.get('checks',{}).values())):
            issues.append('unavailability_reason_not_grounded:'+scope['url'])
    observed={normalized(r['url']) for r in requests if r.get('HTTP_attempted') or r.get('status') is not None}
    urls=[]
    for group in plan['targets']:
        for target in group:
            exact=normalized(target['url']) in observed
            urls.append(dict(**target,exact_URL_observed=exact,
                disposition='observed' if exact else 'inherited_access_hold' if inherited else 'requires_scope_or_projection_reconciliation'))
    # These flags deliberately demand review: a common handler cannot interpret
    # every free-form condition, or prove that a related endpoint is equivalent.
    review=[]
    if result.get('status')=='confirmed':review.append('strategy_and_verbatim_conditions_require_semantic_signoff')
    if any(not u['exact_URL_observed'] for u in urls) and not inherited:review.append('unrequested_or_transformed_URLs_require_reconciliation')
    if plan.get('unresolved_URLs'):review.append('unresolved_requested_URLs')
    return dict(site_id=plan['site_id'],delivery=plan['delivery'],kind=plan['kind'],
        live_check=plan['live_check'],status=result.get('status'),strategy=result.get('strategy'),
        confidence=result.get('confidence'),integrity_issues=issues,review_flags=review,
        required_URLs=urls,unresolved_URLs=plan.get('unresolved_URLs',[]),
        inherited_access_evidence=inherited,reason=result.get('reason'),
        required_checks=[dict(url=s['url'],status=s['status'],checks=s['checks'],
                             reason=s.get('reason'),limitations=s.get('limitations',[])) for s in scopes])


def audit(directory):
    plans=json.loads((directory/'plans.json').read_text())
    queue=json.loads((directory/'input-queue.json').read_text())
    rows=[];pending=[];issues=[]
    if len(queue)!=615 or len({r['site_id'] for r in queue})!=615 or sum(r['delivery'] is True for r in queue)!=363:
        issues.append('queue_scope_mismatch')
    if hashlib.sha256((directory/'input-queue.json').read_bytes()).hexdigest()!=plans['queue_sha256']:
        issues.append('queue_hash_mismatch')
    if {r['site_id'] for r in plans['sites']}!={r['site_id'] for r in queue}:issues.append('plan_scope_mismatch')
    for plan in plans['sites']:
        path=directory/'sites'/(plan['site_id']+'.json')
        if not path.exists():pending.append(plan['site_id']);continue
        row=audit_site(plan,json.loads(path.read_text()));row['evidence_file']=str(path)
        row['evidence_sha256']=hashlib.sha256(path.read_bytes()).hexdigest();rows.append(row)
    result=dict(checked_at=utc_now(),total=615,processed=len(rows),pending=pending,
        queue_issues=issues,integrity_issue_sites=sum(bool(r['integrity_issues']) for r in rows),
        semantic_review_sites=sum(bool(r['review_flags']) for r in rows),
        counts=dict(Counter(r['status'] for r in rows)),sites=rows,
        all_sites_have_result=not pending,goal_completion_proven=False,
        limitation='Integrity and coverage ledger only. Review verbatim conditions, pilot repairs and strategy claims before completion.')
    atomic_json(directory/'completion-audit.json',result)
    return {k:v for k,v in result.items() if k not in ('sites','pending')}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--directory',required=True,type=Path)
    args=parser.parse_args();print(json.dumps(audit(args.directory),ensure_ascii=False))


if __name__=='__main__':main()
