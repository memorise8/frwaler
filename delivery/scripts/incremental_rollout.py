"""Build and reconcile the rollout queue without treating pending as classified.

Only original connected adapters, explicit evidence-backed holds, and accepted
batch reviews close rows. Generic 'adapter missing' is never a terminal result.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from crawler.sites import CRAWLERS
from crawler.incremental_adapters import HAL_SCOPES
from delivery.health.results import atomic_json, utc_now


def build(ledger, previous=None):
    previous={r['site_id']:r for r in (previous or {}).get('sites',[])}
    catalogue={r['site_id']:r for r in ledger['sites']}
    rows=[]
    for site,cls in sorted(CRAWLERS.items()):
        old=catalogue.get(site,{})
        count=old.get('reviewed_query_reported_total') or old.get('enumerable_total') or 0
        try:count=int(count)
        except (TypeError,ValueError):count=0
        path=Path(cls.crawl.__code__.co_filename)
        row={'site_id':site,'delivery':str(old.get('delivery','')).lower()=='true',
             'priority_count':count,'priority_count_quality':'reviewed_scope_count' if count else 'unknown',
             'source_file':str(path),'source_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
             'status':'pending','reason':'requires_adapter_or_evidence_backed_ineligibility_review'}
        if site in HAL_SCOPES or site=='doaj-org-search':
            row.update(status='connected',reason='fd420d8_common_adapter',
                       evidence='incremental-implementation-20261011/summary.json',batch='baseline-fd420d8')
        if site in ('ntrs','ntrs-nasa-gov-search'):
            row.update(status='classified',classification='full_rescan_required',
                       reason='live_latest_order_not_verified; no_safe_early_stop',
                       evidence='incremental-implementation-20261011/probe/probe.json',batch='baseline-fd420d8')
        if site=='cds-cern-ch-collection':
            row.update(status='classified',classification='full_rescan_required',
                       reason='original_collection_access_challenge; proxy_membership_equivalence_unverified',
                       evidence='incremental-implementation-20261011/probe/probe.json',batch='baseline-fd420d8')
        if site in previous and previous[site].get('source_sha256')==row['source_sha256']:
            row.update({k:v for k,v in previous[site].items()
                        if k in ('status','reason','classification','evidence','batch','commit')})
        rows.append(row)
    rows.sort(key=lambda r:(not r['delivery'],-r['priority_count'],r['site_id']))
    return {'updated_at':utc_now(),'goal_status':'active','registered':len(rows),
            'counts':dict(Counter(r['status'] for r in rows)),'sites':rows,
            'completion_rule':'Every current registry row connected or evidence-backed classified; generic missing adapters stay pending.'}


def accept(report,review):
    if review.get('status')!='passed' or not review.get('commit') or not review.get('unit_tests') or not review.get('isolated_request_measurement'):
        raise ValueError('batch acceptance requires commit, tests and isolated request measurements')
    by_id={r['site_id']:r for r in report['sites']}
    for item in review['sites']:
        if item['status'] not in ('connected','classified') or not item.get('reason'):
            raise ValueError('invalid terminal rollout row')
        row=by_id[item['site_id']]
        if item.get('source_sha256')!=row['source_sha256']:
            raise ValueError('batch reviewed a different source')
        row.update(item,batch=review['batch'],commit=review['commit'],evidence=review['evidence'])
    report['counts']=dict(Counter(r['status'] for r in report['sites']))
    report['updated_at']=utc_now()
    return report


def main():
    p=argparse.ArgumentParser();p.add_argument('--base',required=True);p.add_argument('--review');args=p.parse_args()
    base=Path(args.base);root=base/'incremental-rollout-20261012';root.mkdir(exist_ok=True)
    ledger_path=base/'continuous-20261010/site-progress.json';ledger=json.loads(ledger_path.read_text())
    path=root/'queue.json';previous=json.loads(path.read_text()) if path.exists() else None
    report=build(ledger,previous)
    if args.review:report=accept(report,json.loads(Path(args.review).read_text()))
    atomic_json(path,report)
    progress_path=base/'progress.json';progress=json.loads(progress_path.read_text())
    progress['incremental_rollout']={'goal_status':'active','queue':str(path.relative_to(base)),
        'counts':report['counts'],'updated_at':report['updated_at'],
        'previous_goal_turn_classification':'progress',
        'note':'기존 전수검증 goal을 재개한 것이 아니라 공통 증분 연결 goal을 진행 중이다.'}
    by_id={r['site_id']:r for r in report['sites']}
    from delivery.incremental_status import eligibility
    for row in ledger['sites']:
        entry=by_id[row['site_id']]
        row['incremental_rollout']=entry
        if entry.get('commit'):
            row.setdefault('incremental_collection',{}).update(eligibility(row['site_id'],CRAWLERS[row['site_id']]))
            row['incremental_collection']['implementation_commit']=entry['commit']
            row['incremental_collection']['rollout_evidence']=entry['evidence']
    ledger['incremental_rollout_updated_at']=report['updated_at']
    atomic_json(ledger_path,ledger);atomic_json(progress_path,progress)
    print(json.dumps({'counts':report['counts'],'next':[r['site_id'] for r in report['sites'] if r['status']=='pending'][:10]}))

if __name__=='__main__':main()
