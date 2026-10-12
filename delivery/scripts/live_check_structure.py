"""Priority live-check runner. No collector instances, DB connections or documents."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import socket
from urllib.parse import urlsplit

from delivery.health.results import atomic_json, utc_now
from delivery.scripts.live_check_contracts import CHECKS, KINDS, compile_plan
from delivery.scripts.live_check_probes import ProbeSession, probe_target
from delivery.scripts.live_check_transport import LiveTransport, LiveStop

ROOT=Path(__file__).resolve().parents[2]
DEFAULT_QUEUE=Path('/tmp/claude-1001/-mnt-raid-ruci-workspace/af50b2cc-5966-4982-9f45-a0737ed1cb06/scratchpad/live-check-queue-prioritized.json')


def prepare(out, prior, queue):
    from crawler.sites import CRAWLERS
    source=json.loads(queue.read_text())
    if len(source)!=615 or len({x['site_id'] for x in source})!=615 or sum(x['delivery'] is True for x in source)!=363:
        raise ValueError('expected 615 unique sites and 363 delivery sites')
    if any(x['kind'] not in KINDS or x['site_id'] not in CRAWLERS for x in source):raise ValueError('unknown queue kind or site')
    manifest={x['site_id']:x for x in json.loads((prior/'manifest.json').read_text())['sites']}
    hints=json.loads((prior/'source-hints.json').read_text())['sites'];plans=[]
    for row in source:
        cls=CRAWLERS[row['site_id']];path=Path(cls.crawl.__code__.co_filename)
        cp=getattr(cls,'_config_path',None);config=json.loads(Path(cp).read_text()) if cp else None
        plan=compile_plan(row,manifest[row['site_id']],hints[row['site_id']],path.read_text(),config)
        plan['source_file']=str(path);plans.append(plan)
    plans.sort(key=lambda r:(not r['delivery'],KINDS.index(r['kind']),r['site_id']))
    (out/'input-queue.json').write_bytes(queue.read_bytes())
    data=dict(created_at=utc_now(),queue_source=str(queue),queue_sha256=hashlib.sha256(queue.read_bytes()).hexdigest(),
              registered=615,delivery=363,priority=list(KINDS),sites=plans)
    atomic_json(out/'plans.json',data);return data


def process(plan, transport, out, budget):
    site=plan['site_id'];path=out/'attempts'/(site+'.json')
    attempts=json.loads(path.read_text())['requests'] if path.exists() else []
    result=dict(site_id=site,delivery=plan['delivery'],kind=plan['kind'],live_check=plan['live_check'],
      started_at=utc_now(),server=socket.gethostname(),status='running',scopes=[],requests=attempts,
      request_budget=budget,documents_saved=0,database_connections=0,source_file=plan['source_file'],
      source_sha256=plan['source_sha256'],static_limits=plan['static_limits'],unresolved_URLs=plan['unresolved_URLs'],
      strategy='불가',confidence='낮음',instruction_coverage='not_yet_verified')
    def persist():atomic_json(path,result)
    persist()
    hosts={urlsplit(url).hostname for url in plan['original_scope_urls']}
    previous_path=transport.prior/'sites'/(site+'.json')
    if previous_path.exists():
        previous=json.loads(previous_path.read_text())
        for scope in previous.get('scopes',[]):
            hosts.update(urlsplit(scope[key]).hostname for key in ('url','resolved_url') if scope.get(key))
    holds={h:transport.held[h] for h in hosts if h in transport.held and transport.held[h].get('reason')!='rate_limited'}
    if holds:
        result.update(status='access_blocked',reason='previous_source_refusal_not_bypassed',inherited_access_evidence=holds,
                      instruction_coverage='all checks unavailable under observed source access hold')
        result['unavailable_checks']={name:dict(status='access_blocked',reason=result['reason']) for name in CHECKS}
    else:
        session=ProbeSession(plan,transport,result,persist)
        for target in plan['targets']:
            if (out/'STOP').exists():raise LiveStop()
            scope=probe_target(session,target);result['scopes'].append(scope);persist()
            if scope['status'] in ('access_blocked','unconfirmable_budget'):
                result['unvisited_target_groups']=len(plan['targets'])-len(result['scopes']);break
        states=[s['status'] for s in result['scopes']]
        if not states:result.update(status='unconfirmable_custom',reason='no_concrete_public_entry_or_endpoint_in_queue_and_source')
        elif 'access_blocked' in states:result.update(status='access_blocked',reason='access_or_robots_policy_prevents_requested_checks')
        elif all(s=='confirmed' for s in states) and len(states)==len(plan['targets']) and not plan['unresolved_URLs']:
            # Prose can ask for union counts, POST payloads or cross-site
            # equivalence beyond the common boundary probes. Keep those open.
            special=any(x in plan['live_check'] for x in ('연도별 합계','전체 합','중복 여부','POST','GraphQL','graphql','1947','XHR'))
            if special:result.update(status='unconfirmable_custom',reason='additional_prose_condition_requires_custom_reconciliation')
            else:result.update(status='confirmed',strategy=result['scopes'][0]['strategy'],
                confidence='높음' if all(s['confidence']=='높음' for s in result['scopes']) else '보통')
        else:result.update(status='unconfirmable_custom',reason='common_probe_cannot_confirm_all_requested_scopes_or_conditions')
        result['instruction_coverage']='See all target scopes and original live_check; unsupported conditions remain explicitly unconfirmed'
        result['partial_strategies']=sorted({s['strategy'] for s in result['scopes'] if s['strategy']!='불가'})
    result.update(finished_at=utc_now(),actual_HTTP_responses=sum(x.get('status') is not None for x in result['requests']))
    persist();return result


def defects(results):
    findings=[]
    for result in results:
        site=result['site_id']
        for cap in result.get('static_limits',[]):
            number=cap.get('value') if cap['kind'].startswith('page_cap_') else cap.get('maximum_number')
            if number==200:
                findings.append(dict(site_id=site,kind='collector_200_page_cap_candidate',status='static_risk_not_yet_proven_truncation',evidence=cap,source_file=result['source_file']))
        for scope in result.get('scopes',[]):
            checks=scope['checks'];last=checks['last_and_after'].get('last')
            for cap in result.get('static_limits',[]):
                number=cap.get('value') if cap['kind'].startswith('page_cap_') else cap.get('maximum_number')
                if type(number) is int and type(last) is int and last>number:
                    findings.append(dict(site_id=site,kind='collector_cap_below_observed_last_page',status='requires_code_route_binding',
                                         observed_last=last,cap=cap,scope=scope['url']))
            hidden=checks['hidden_cap']
            if hidden.get('status','').startswith('observed_'):
                findings.append(dict(site_id=site,kind='page_ignored_wraparound_or_clamp',status='source_behavior_observed_collector_impact_requires_review',scope=scope['url'],evidence=hidden))
            if checks['first_page_number'].get('first')==0:
                for cap in result.get('static_limits',[]):
                    if cap.get('start')==1 or cap['kind']=='config_pagination' and cap['value'].get('start')==1:
                        findings.append(dict(site_id=site,kind='first_page_omission_candidate',status='requires_code_route_binding',scope=scope['url'],evidence=cap))
    targets={}
    for result in results:
        for scope in result.get('scopes',[]):
            migration=scope['checks']['domain_migration'];url=migration.get('resolved_url')
            if url:targets.setdefault(url,[]).append((result['site_id'],migration.get('old_domain_redirect_observed',False)))
    for url,entries in targets.items():
        sites=sorted({s for s,_ in entries})
        if len(sites)>1 and any(changed for _,changed in entries):
            findings.append(dict(site_ids=sites,kind='old_domain_duplicate_registration_candidate',status='same_redirect_target_scope_equivalence_unproven',resolved_url=url))
    return findings


def summarize(out, base, data):
    results=[]
    for row in data['sites']:
        path=out/'sites'/(row['site_id']+'.json')
        if path.exists():results.append(json.loads(path.read_text()))
    summary=dict(updated_at=utc_now(),total=615,processed=len(results),remaining=615-len(results),
        delivery_processed=sum(r['delivery'] for r in results),delivery_total=363,
        counts=dict(Counter(r['status'] for r in results)),strategies=dict(Counter(r['strategy'] for r in results)),
        confidences=dict(Counter(r['confidence'] for r in results)),requests_recorded=sum(len(r['requests']) for r in results),
        HTTP_responses=sum(r['actual_HTTP_responses'] for r in results),documents_saved=0,database_connections=0,
        all_sites_processed=len(results)==615,goal_complete=False)
    atomic_json(out/'summary.json',summary);atomic_json(out/'collector-defects.json',dict(updated_at=summary['updated_at'],findings=defects(results)))
    buffer=io.StringIO();fields=['site_id','delivery','kind','status','strategy','confidence','requests','reason','evidence']
    writer=csv.DictWriter(buffer,fieldnames=fields);writer.writeheader()
    for r in results:writer.writerow({**{k:r[k] for k in fields[:6]},'requests':len(r['requests']),'reason':r.get('reason',''),'evidence':str(out/'sites'/(r['site_id']+'.json'))})
    temp=out/'summary.csv.tmp';temp.write_text(buffer.getvalue());os.replace(temp,out/'summary.csv')
    path=base/'progress.json';progress=json.loads(path.read_text());progress['live_structure_check']={**summary,'directory':str(out)};atomic_json(path,progress)
    path=base/'continuous-20261010/site-progress.json';ledger=json.loads(path.read_text());mapping={x['site_id']:x for x in results}
    for row in ledger['sites']:
        if row['site_id'] in mapping:
            r=mapping[row['site_id']];row['live_structure_check']={k:r[k] for k in ('status','strategy','confidence','finished_at','actual_HTTP_responses')}
            row['live_structure_check']['evidence']=str(out/'sites'/(row['site_id']+'.json'))
    atomic_json(path,ledger);return summary


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--base',required=True,type=Path)
    parser.add_argument('--queue',type=Path,default=DEFAULT_QUEUE);parser.add_argument('--workers',type=int,default=8)
    parser.add_argument('--limit',type=int);parser.add_argument('--budget',type=int,default=30)
    parser.add_argument('--refresh-plans',action='store_true')
    args=parser.parse_args()
    if not 1<=args.workers<=8 or not 8<=args.budget<=30:raise ValueError('bounded worker/request limits required')
    out=args.base/'live-check-20261012';out.mkdir(parents=True,exist_ok=True);prior=args.base/'discovery-20261012'
    with (out/'runner.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.refresh_plans and (out/'plans.json').exists():
            (out/'plans.previous.json').write_bytes((out/'plans.json').read_bytes())
        data=json.loads((out/'plans.json').read_text()) if (out/'plans.json').exists() and not args.refresh_plans else prepare(out,prior,args.queue)
        transport=LiveTransport(out,prior)
        pending=[r for r in data['sites'] if not (out/'sites'/(r['site_id']+'.json')).exists()]
        if args.limit:pending=pending[:args.limit]
        state=dict(pid=os.getpid(),server=socket.gethostname(),status='running',started_at=utc_now(),scheduled=len(pending),finished=0,workers=args.workers,
             code_sha256={p:hashlib.sha256((ROOT/'delivery/scripts'/p).read_bytes()).hexdigest() for p in ('live_check_structure.py','live_check_probes.py','live_check_contracts.py','live_check_transport.py')})
        atomic_json(out/'runner.json',state)
        def run(plan):
            if (out/'STOP').exists():return None
            try:return process(plan,transport,out,args.budget)
            except LiveStop:return None
            except Exception as exc:
                path=out/'attempts'/(plan['site_id']+'.json')
                result=json.loads(path.read_text()) if path.exists() else dict(site_id=plan['site_id'],requests=[])
                result.update(status='runner_error',reason=type(exc).__name__+': '+str(exc)[:300],finished_at=utc_now(),actual_HTTP_responses=sum(x.get('status') is not None for x in result['requests']))
                return result
        try:
            # Complete delivery-priority work before starting the non-delivery
            # tier; within a tier, the submitted queue follows kind priority.
            for delivery in (True,False):
                batch=[r for r in pending if r['delivery']==delivery]
                with ThreadPoolExecutor(max_workers=args.workers) as pool:
                    for future in as_completed([pool.submit(run,row) for row in batch]):
                        result=future.result()
                        if result is None:continue
                        atomic_json(out/'sites'/(result['site_id']+'.json'),result)
                        state.update(finished=state['finished']+1,last_site=result['site_id'],updated_at=utc_now());atomic_json(out/'runner.json',state)
                        if state['finished']%10==0:print(json.dumps(summarize(out,args.base,data),ensure_ascii=False),flush=True)
            state['status']='stopped' if (out/'STOP').exists() else 'finished'
        finally:
            if state['status']=='running':state['status']='interrupted_or_failed'
            state['finished_at']=utc_now();atomic_json(out/'runner.json',state)
            print(json.dumps(summarize(out,args.base,data),ensure_ascii=False),flush=True)


if __name__=='__main__':main()
