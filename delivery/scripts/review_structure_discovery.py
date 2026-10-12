"""Offline evidence-quality audit and follow-up queue. No network or DB access.

The first-pass runner may continue writing other sites. This command writes an
overlay only; it never races with runner progress, ledgers or site JSON files.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from delivery.health.results import atomic_json, utc_now
from delivery.scripts.discover_collection_structure import FEATURES


def audit_site(result):
    problems=[];routes=[]
    responses={r.get('number'):r for r in result.get('requests',[])}
    if len(responses)>result.get('request_budget',24):problems.append('request_budget_exceeded')
    if result.get('documents_saved',0) or result.get('database_connections',0):problems.append('recon_only_invariant_failed')
    if not result.get('scopes') and result.get('reason')=='runner_error_not_source_inability':
        problems.append('runner_error_requires_repair')
    for index,scope in enumerate(result.get('scopes',[])):
        features=scope.get('features',{});issues=[];methods=[]
        missing=set(FEATURES)-features.keys()
        if missing:issues.append('missing_required_features:'+','.join(sorted(missing)))
        if scope.get('status')=='unmeasurable':
            reasons=scope.get('blocked_reasons',[])
            if not reasons:issues.append('unmeasurable_without_observed_reason')
            if any('redirect_scope_requires_review' in r or r.startswith('parser_') for r in reasons):
                issues.append('redirect_or_parser_problem_not_final_inability')
                methods.append('inspect_redirect_or_retained_HTML_before_optional_browser')
        else:
            first=responses.get(scope.get('first_response'),{}).get('summary',{})
            oldest=features.get('oldest_item',{}).get('item') or {}
            boundary=features.get('last_page',{})
            last=responses.get(boundary.get('response'),{}).get('summary',{})
            if not oldest.get('id'):issues.append('oldest_identity_unresolved')
            if not oldest.get('date'):issues.append('oldest_date_unresolved')
            if oldest.get('date_basis') in ('row_text_unverified_date_role','sitemap_lastmod_not_publication'):
                issues.append('oldest_date_role_unverified')
            if boundary.get('verified') and last.get('date_order',{}).get('missing'):
                issues.append('last_page_contains_undated_items_oldest_date_not_established')
                methods.append('parse_retained_last_page_date_controls_then_independent_date_filter')
            if boundary.get('verified') and oldest.get('id') and oldest.get('id') not in {i['id'] for i in last.get('items',[])}:
                # Samples retain first/last eight. A missing key is suspicious,
                # not proof of an incorrect result when a page is longer.
                issues.append('oldest_sample_not_evidenced_on_last_page')
            order=first.get('date_order',{})
            if order.get('increasing') and order.get('decreasing'):
                issues.append('mixed_date_order_requires_pin_or_sort_review')
            if first.get('duplicate_id_count') or last.get('duplicate_id_count'):
                issues.append('repeated_identity_requires_scope_review')
            if not first.get('items'):
                methods.append('review_existing_source_parser_and_retained_markup')
                if first.get('javascript_shell'):methods.append('read_only_Playwright_without_security_challenge_bypass')
            sort=features.get('oldest_sort',{})
            if sort.get('verified') and not scope.get('cross_checks'):
                issues.append('sort_has_no_independent_comparison')
            limits=features.get('limits_and_wraparound',{})
            if limits.get('wraparound_or_clamp'):
                issues.append('wraparound_or_cap_needs_second_method')
                methods.append('advertised_date_filter_or_scope_verified_sitemap')
            if not boundary.get('verified') and not sort.get('verified'):
                issues.append('backfill_route_not_verified')
                if features.get('date_filters',{}).get('controls'):methods.append('bounded_execution_of_published_date_filter')
                if features.get('independent_evidence',{}).get('sitemap_links'):methods.append('scope_reconciliation_against_advertised_sitemap')
            if features.get('identity_stability',{}).get('same_ordered_IDs_on_repeat') is False:
                issues.append('identity_or_order_changed_on_repeat')
        if issues:
            problems.extend(f'scope_{index}:{i}' for i in issues)
            routes.append(dict(scope_index=index,url=scope['url'],issues=issues,
                proposed_methods=list(dict.fromkeys(methods)) or ['inspect_recorded_response_summaries_and_original_scope'],
                security_or_login_hold_must_not_be_bypassed=True))
    if not result.get('server') or not result.get('finished_at'):problems.append('measurement_origin_missing')
    disposition='needs_review' if problems or result['status']=='needs_review' else result['status']
    return dict(site_id=result['site_id'],first_pass_status=result['status'],audited_status=disposition,
        issues=problems,followup_routes=routes,requests_already_used=len(result.get('requests',[])),
        remaining_requests_before_30=max(0,30-len(result.get('requests',[]))))


def review(out):
    out=Path(out);rows=[]
    for path in sorted((out/'sites').glob('*.json')):
        raw=path.read_bytes();row=audit_site(json.loads(raw));row['source_result_sha256']=hashlib.sha256(raw).hexdigest();rows.append(row)
    findings=Counter(reason.split(':',1)[-1] for row in rows for reason in row['issues'])
    result=dict(reviewed_at=utc_now(),method='offline_metadata_evidence_audit',network_requests=0,database_connections=0,
        sites_reviewed=len(rows),registered=1245,not_yet_reviewable=1245-len(rows),
        counts=dict(Counter(r['audited_status'] for r in rows)),issue_counts=dict(findings),sites=rows,
        completion_verified=False,overlay_only=True)
    atomic_json(out/'quality-review.json',result)
    atomic_json(out/'followup-queue.json',dict(updated_at=result['reviewed_at'],sites=[r for r in rows if r['audited_status']=='needs_review']))
    print(json.dumps({k:result[k] for k in ('sites_reviewed','counts','issue_counts')},ensure_ascii=False))
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);args=p.parse_args();review(args.out)
