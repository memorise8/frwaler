"""Reconcile concrete instruction URL omissions without resetting request budgets.

The first-pass runner must finish before --execute can acquire its exclusive
lock. Dry-run is offline and writes a proposed queue only. Original metadata
is archived before a selected result is replaced. No documents or DB calls.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile

from delivery.health.results import atomic_json, utc_now
from delivery.scripts.audit_live_structure import normalized
from delivery.scripts.live_check_contracts import page_route
from delivery.scripts.live_check_structure import prepare, process, summarize
from delivery.scripts.live_check_transport import LiveTransport, LiveStop


PILOTS={'academie-sciences-fr-espace-presse','acpr-banque-france-fr-fr','afp-gov-au-news-centre',
        'afp-gov-au-search','alrc-gov-au-publications','app-mps-gov-cn-gdnps','arn-se-om-arn','asiapacific-ca-media'}


def repair_queue(data,out):
    selected=[];excluded=[]
    for plan in data['sites']:
        site=plan['site_id'];path=out/'sites'/(site+'.json')
        if not path.exists():excluded.append(dict(site_id=site,reason='first_pass_pending'));continue
        result=json.loads(path.read_text());requests=result['requests']
        if result.get('repair'):
            excluded.append(dict(site_id=site,reason='already_repaired_requires_completion_audit'));continue
        attempted=out/'attempts'/(site+'.json')
        if attempted.exists():
            history=json.loads(attempted.read_text()).get('requests',[])
            if len(history)>len(requests):requests=history
        seen={normalized(r['url']) for r in requests}
        missing=[t for group in plan['targets'] for t in group if normalized(t['url']) not in seen and t['provenance']!='original_registered_scope']
        if result['status']=='access_blocked':
            excluded.append(dict(site_id=site,reason='preserve_access_or_robots_hold'));continue
        if len(requests)>=30:
            excluded.append(dict(site_id=site,reason='cumulative_request_budget_already_spent'));continue
        # Parameter-only WP projection differences are reviewed as evidence,
        # not blindly re-fetched when the exact target was already a scope.
        scope_urls={normalized(s['url']) for s in result['scopes']}
        missing=[t for t in missing if normalized(t['url']) not in scope_urls]
        json_boundary=any(r.get('summary',{}).get('format')=='json' and r['summary'].get('item_count')
            and '/wp-json/' not in r['url'] and page_route(r['url'],plan['live_check']) for r in requests)
        if missing or site in PILOTS or json_boundary or result['status']=='runner_error':
            selected.append(dict(site_id=site,delivery=plan['delivery'],kind=plan['kind'],
                prior_requests=len(requests),remaining_budget=30-len(requests),
                missing_concrete_URLs=missing,pilot_parser_review=site in PILOTS,
                typed_JSON_boundary_review=bool(json_boundary),
                prior_result_sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    return dict(created_at=utc_now(),sites=selected,excluded=excluded,
                total_selected=len(selected),delivery_selected=sum(r['delivery'] for r in selected))


def run(base,execute=False,workers=8):
    out=base/'live-check-20261012';prior=base/'discovery-20261012'
    # Compile in a temporary directory; never rewrite a live runner's plans.
    with tempfile.TemporaryDirectory() as tmp:
        data=prepare(Path(tmp),prior,out/'input-queue.json')
    proposed=repair_queue(data,out)
    atomic_json(out/'repair-queue.json',proposed)
    atomic_json(out/'repair-plans.json',data)
    if not execute:return {k:v for k,v in proposed.items() if k not in ('sites','excluded')}
    with (out/'runner.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        original=json.loads((out/'runner.json').read_text())
        if original['status']!='finished' or any(not (out/'sites'/(p['site_id']+'.json')).exists() for p in data['sites']):
            raise RuntimeError('finish all 615 first-pass results before repair execution')
        archive=out/'before-repair';archive.mkdir(exist_ok=True)
        if not (archive/'plans.json').exists():(archive/'plans.json').write_bytes((out/'plans.json').read_bytes())
        atomic_json(out/'plans.json',data)
        mapping={p['site_id']:p for p in data['sites']};transport=LiveTransport(out,prior)
        state=dict(pid=os.getpid(),server=socket.gethostname(),status='running',started_at=utc_now(),
            selected=proposed['total_selected'],finished=0,
            code_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob('*live*.py')})
        atomic_json(out/'repair-runner.json',state)
        def work(item):
            site=item['site_id'];path=out/'sites'/(site+'.json')
            if hashlib.sha256(path.read_bytes()).hexdigest()!=item['prior_result_sha256']:
                raise RuntimeError('result changed since repair planning: '+site)
            archived=archive/(site+'.json')
            if not archived.exists():archived.write_bytes(path.read_bytes())
            plan=mapping[site]
            # Visit newly compiled, explicitly requested URL groups before old
            # landing pages, so a parser failure cannot hide a named endpoint.
            missing={t['url'] for t in item['missing_concrete_URLs']}
            targets=[]
            for group in plan['targets']:
                targets.append(sorted(group,key=lambda t:t['url'] not in missing))
            plan={**plan,'targets':sorted(targets,key=lambda g:not any(t['url'] in missing for t in g))}
            try:
                result=process(plan,transport,out,30,seed_summaries=site not in PILOTS)
            except LiveStop:return None
            result['repair']=dict(previous_result=str(archive/(site+'.json')),previous_requests=item['prior_requests'],
                                  new_requests=len(result['requests'])-item['prior_requests'],reasons=item)
            atomic_json(path,result);return result
        try:
            for delivery in (True,False):
                batch=[r for r in proposed['sites'] if r['delivery']==delivery]
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    for future in as_completed([pool.submit(work,row) for row in batch]):
                        result=future.result()
                        if result is None:continue
                        state.update(finished=state['finished']+1,last_site=result['site_id'],updated_at=utc_now())
                        atomic_json(out/'repair-runner.json',state)
                        if state['finished']%10==0:print(json.dumps(summarize(out,base,data),ensure_ascii=False),flush=True)
            state['status']='stopped' if (out/'STOP').exists() else 'finished'
        finally:
            if state['status']=='running':state['status']='interrupted_or_failed'
            state['finished_at']=utc_now();atomic_json(out/'repair-runner.json',state)
            summarize(out,base,data)
        return state


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--base',required=True,type=Path)
    parser.add_argument('--execute',action='store_true');parser.add_argument('--workers',type=int,default=8)
    args=parser.parse_args()
    if not 1<=args.workers<=8:raise ValueError('worker bound exceeded')
    print(json.dumps(run(args.base,args.execute,args.workers),ensure_ascii=False))


if __name__=='__main__':main()
