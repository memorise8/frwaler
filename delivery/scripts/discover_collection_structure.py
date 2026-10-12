"""Bounded reconnaissance runner. Only JSON/CSV metadata is persisted; never crawl()."""
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
import threading
import time
from urllib.parse import urljoin, urlsplit

import requests
from delivery.health.access_challenge import access_challenge
from delivery.health.request_gate import RequestGate, NetworkDeferred
from delivery.health.results import atomic_json, utc_now
from delivery.health.scope_inventory import public_url, static_profile
from delivery.health.source_observer import public_address, retry_after
from delivery.scripts.discovery_structure import inspect, page_parameter, query_replace, same_listing

ROOT=Path(__file__).resolve().parents[2]
DEFAULT_TIERS=Path('/tmp/claude-1001/-mnt-raid-ruci-workspace/af50b2cc-5966-4982-9f45-a0737ed1cb06/scratchpad/site_tiers.csv')
MAX_BODY=2*1024*1024
FEATURES=('oldest_sort','last_page','date_filters','limits_and_wraparound','oldest_item','independent_evidence','identity_stability','measurement_origin')


class StopRequested(Exception):
    pass


def manifest(tiers):
    from crawler.sites import CRAWLERS
    with Path(tiers).open(encoding='utf-8-sig') as f:rows=list(csv.DictReader(f))
    if len(rows)!=1245 or len({r['site_id'] for r in rows})!=1245 or {r['site_id'] for r in rows}!=set(CRAWLERS):
        raise ValueError('tier CSV and 1245 registered sites must match exactly')
    scopes={}
    with (ROOT/'docs/deliverables/test_URL_수집대조_20260802.csv').open(encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            site=row['매칭크롤러(site_id)'];url=public_url(row['URL주소'].strip())
            if url and url not in scopes.setdefault(site,[]):scopes[site].append(url)
    for row in rows:
        cls=CRAWLERS[row['site_id']];path=Path(cls.crawl.__code__.co_filename)
        configpath=getattr(cls,'_config_path',None);config=json.loads(Path(configpath).read_text()) if configpath else None
        profile=static_profile(path.read_text(),config=config,base_url=getattr(cls,'base_url',None))
        original=scopes.get(row['site_id'],[])
        selected=profile.get('selected_entry')
        row.update(initial_tier=row.pop('tier'),profile=profile,source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                   scopes=[dict(url=u,provenance='original_delivery_CSV') for u in original] or
                          ([dict(url=selected['url'],provenance=selected['role'])] if selected else []))
    rows.sort(key=lambda r:(r['delivery'].lower()!='true',r['site_id']))
    return dict(created_at=utc_now(),registered=1245,tiers_file=str(tiers),tiers_sha256=hashlib.sha256(Path(tiers).read_bytes()).hexdigest(),sites=rows)


class ReconTransport:
    def __init__(self,out,base):
        self.out=Path(out);self.base=Path(base);self.lock=threading.Lock();self.gates={};self.held={}
        # Preserve access refusals across restarts; never rotate clients to bypass them.
        path=self.out/'access-holds.json'
        if path.exists():self.held=json.loads(path.read_text())
    def gate(self,host):
        with self.lock:
            return self.gates.setdefault(host,RequestGate(self.out/'request-gate'/hashlib.sha256(host.encode()).hexdigest()[:20],delay=1.5))
    def hold(self,host,evidence):
        with self.lock:
            self.held[host]=evidence;atomic_json(self.out/'access-holds.json',self.held)
    def fetch(self,url,*,inspect_error_json=False):
        host=urlsplit(url).hostname
        if not public_url(url):return dict(url=url,status=None,error='not_public_literal_URL',checked_at=utc_now()),None
        with self.lock:held=self.held.get(host)
        if held:return dict(url=url,status=None,error='host_access_hold',hold_evidence=held,checked_at=utc_now()),None
        gate=self.gate(host);response=dict(url=url,checked_at=utc_now(),method='GET',server=socket.gethostname())
        try:
            with gate.enter(url):
                with self.lock:held=self.held.get(host)
                if held:return dict(response,error='host_access_hold',hold_evidence=held),None
                address=public_address(host,urlsplit(url).port or (443 if url.startswith('https:') else 80))
                response['resolved_public_address']=address
                session=requests.Session();session.trust_env=False
                try:
                    with session.get(url,stream=True,timeout=(10,25),allow_redirects=False,
                                     headers={'User-Agent':'Libertree-Structure-Recon/1.0','Accept-Encoding':'gzip, deflate',
                                              'Accept':'text/html,application/json,application/xml;q=0.9,*/*;q=0.1'}) as r:
                        ct=r.headers.get('Content-Type','');response.update(status=r.status_code,content_type=ct,HTTP_attempted=True)
                        response['source_headers']={name:r.headers[name] for name in ('X-WP-Total','X-WP-TotalPages','Date') if name in r.headers}
                        if r.is_redirect:
                            response['redirect']=public_url(urljoin(url,r.headers.get('Location','')))
                            return response,None
                        if r.status_code in (401,403,407,429,451):
                            reason='authentication_required' if r.status_code in (401,407) else 'access_refused' if r.status_code in (403,451) else 'rate_limited'
                            response['error']=reason;self.hold(host,dict(url=url,status=r.status_code,reason=reason,at=response['checked_at']))
                            if r.status_code==429:gate.defer(url,retry_after(r.headers.get('Retry-After')))
                            return response,None
                        if r.status_code>=400:
                            response['error']='HTTP_'+str(r.status_code)
                            if not (inspect_error_json and r.status_code==400 and 'json' in ct.lower()):return response,None
                        if not any(t in ct.lower() for t in ('html','json','xml','text/plain')):
                            response['error']='non_listing_content_type_body_not_downloaded';return response,None
                        if 'attachment' in r.headers.get('Content-Disposition','').lower():
                            response['error']='attachment_body_not_downloaded';return response,None
                        chunks=[];size=0;began=time.monotonic()
                        for chunk in r.iter_content(32768):
                            size+=len(chunk)
                            if size>MAX_BODY or time.monotonic()-began>30:
                                response.update(error='bounded_listing_response_exceeded',bytes_observed=size);return response,None
                            chunks.append(chunk)
                        raw=b''.join(chunks);response.update(bytes_observed=len(raw),sha256=hashlib.sha256(raw).hexdigest(),body_persisted=False)
                        challenge=access_challenge(raw)
                        # Cloudflare browser checks are a stop condition, not a Playwright target.
                        lower=raw[:131072].lower()
                        if not challenge and (b'<title>just a moment' in lower or b'cf-chl-' in lower and b'challenge-platform' in lower):challenge='browser_security_challenge'
                        if challenge:
                            response['error']=challenge;self.hold(host,dict(url=url,reason=challenge,at=response['checked_at']));return response,None
                        return response,raw
                finally:session.close()
        except NetworkDeferred as exc:response.update(error='request_gate_cooldown',retry_at=exc.retry_at)
        except Exception as exc:response.update(error=type(exc).__name__)
        return response,None


def unknown(reason):return dict(status='unverified',reason=reason)


def scout_site(row,transport,budget=24):
    site=row['site_id'];result=dict(schema_version=1,site_id=site,initial_tier=row['initial_tier'],initial_basis=row['basis'],
        started_at=utc_now(),server=socket.gethostname(),source_sha256=row['source_sha256'],requests=[],scopes=[],
        request_budget=budget,documents_saved=0,database_connections=0,status='running')
    prior_attempts=transport.out/'attempts'/(site+'.json')
    if prior_attempts.exists():
        result['requests']=json.loads(prior_attempts.read_text()).get('requests',[])
        result['resumed_prior_attempts']=len(result['requests'])
    def read(url,purpose,selectors):
        if (transport.out/'STOP').exists():raise StopRequested()
        if len(result['requests'])>=budget:return dict(url=url,error='site_request_budget_exhausted'),None
        response,raw=transport.fetch(url);response['purpose']=purpose;response['number']=len(result['requests'])+1
        result['requests'].append(response)
        atomic_json(transport.out/'attempts'/(site+'.json'),dict(site_id=site,requests=result['requests'],updated_at=utc_now()))
        if raw is None:return response,None
        try:parsed=inspect(raw,url,response.get('content_type',''),selectors)
        except Exception as exc:
            response['error']='parser_'+type(exc).__name__;return response,None
        response['summary']=parsed
        if parsed.get('login_required'):
            response['error']='login_form_required';transport.hold(urlsplit(url).hostname,dict(url=url,reason='login_form_required',at=response['checked_at']));return response,None
        return response,parsed
    for scope in row['scopes']:
        url=scope['url'];selectors=row['profile']['list_selectors'];features={k:unknown('not_yet_observed') for k in FEATURES}
        s=dict(**scope,features=features,strategy='불가',confidence='낮음',blocked_reasons=[],cross_checks=[],status='needs_review')
        result['scopes'].append(s);first_response,first=read(url,'original_scope_entry',selectors)
        features['measurement_origin']=dict(status='observed',server=result['server'],at=first_response.get('checked_at',utc_now()),url=url)
        # Follow at most two advertised canonical redirects, never auth/JS challenges.
        redirects=[]
        while first is None and first_response.get('redirect') and len(redirects)<2:
            target=first_response['redirect']
            if any(x in urlsplit(target).path.lower() for x in ('login','signin','auth','captcha','challenge')):break
            if target in redirects or target==url:break
            redirects.append(target);first_response,first=read(target,'published_redirect',selectors);url=target
        s['resolved_url']=url
        if first is None:
            reason=first_response.get('error') or 'redirect_scope_requires_review'
            s['blocked_reasons']=[reason];s['status']='needs_review' if reason.startswith('parser_') or reason=='site_request_budget_exhausted' else 'unmeasurable'
            for k in FEATURES:
                if k!='measurement_origin':features[k]=dict(status='unmeasurable',reason=reason)
            continue
        s['first_response']=first_response['number'];oldest=first.get('oldest_observed')
        features['date_filters']=dict(status='observed' if first['date_filters'] else 'not_exposed_in_response',controls=first['date_filters'],tested=False)
        features['independent_evidence']=dict(status='observed' if first['totals'] or first['sitemaps'] else 'not_exposed_in_response',reported_counts=first['totals'],sitemap_links=first['sitemaps'],scope_equivalence_verified=False)
        features['oldest_sort']=dict(status='advertised' if first['oldest_options'] else 'not_exposed_in_response',options=first['oldest_options'],verified=False)
        features['last_page']=dict(status='advertised' if first['last_links'] else 'unverified',advertised_links=first['last_links'],verified=False)
        features['identity_stability']=dict(status='single_observation_only',method=first['item_identity_method'],duplicates=first.get('duplicate_id_count',0),long_term_stability_verified=False)
        features['limits_and_wraparound']=unknown('requires_boundary_probe');suspect=not first['items'] or bool(first.get('duplicate_id_count'))
        # Published oldest-order controls only: no invented API parameter names.
        for option in first['oldest_options'][:1]:
            response,ordered=read(option['url'],'advertised_oldest_sort',selectors)
            if ordered:
                order=ordered['date_order'];verified=bool(ordered['items'] and order['increasing'] and not order['decreasing'] and not order['missing'])
                features['oldest_sort'].update(status='measured',verified=verified,method=option,response=response['number'],date_order=order)
                if verified:s.update(strategy='오래된 순 정렬',confidence='보통');oldest=ordered['oldest_observed']
                else:suspect=True
        last_url=first['last_links'][0] if first['last_links'] else None
        # A visible maximum is a candidate only, never proof it is the last page.
        if not last_url and first['pagination']:
            numbered=[p for p in first['pagination'] if p.get('parameter')]
            if numbered:last_url=max(numbered,key=lambda p:p['parameter'][1])['url']
        if last_url and last_url!=url:
            response,last=read(last_url,'last_or_maximum_advertised_page',selectors)
            # Sliding pagers expose more numbers after a jump. Follow those
            # published maxima, never assume the first visible maximum is final.
            for _ in range(4):
                if not last or not last['next_links'] or not page_parameter(last_url):break
                further=[x for x in last['pagination'] if x.get('parameter') and x['parameter'][1]>page_parameter(last_url)[1] and same_listing(url,x['url'])]
                if not further:break
                next_max=max(further,key=lambda x:x['parameter'][1])['url']
                s['cross_checks'].append(dict(method='sliding_pager_exposes_later_numbers',from_url=last_url,to_url=next_max,conclusive=True))
                last_url=next_max;response,last=read(last_url,'follow_published_later_page_maximum',selectors)
            if last:
                same=bool(first['item_fingerprint'] and first['item_fingerprint']==last['item_fingerprint'])
                features['last_page'].update(status='measured',url=last_url,response=response['number'],same_as_first=same,verified=False)
                terminal=not last['next_links'] and not any(page_parameter(x) and page_parameter(last_url) and page_parameter(x)[1]>page_parameter(last_url)[1] for x in last['last_links'])
                parameter=page_parameter(last_url)
                if parameter and parameter[0].lower() not in ('start','offset'):
                    beyond_url=query_replace(last_url,parameter[0],parameter[1]+1)
                    br,beyond=read(beyond_url,'one_page_after_candidate_end',selectors)
                    empty=bool(beyond and beyond['item_count']==0 and last['item_count']>0 and beyond.get('structural_signature') and beyond.get('structural_signature')==last.get('structural_signature') and beyond.get('page_title')==last.get('page_title'))
                    wrapped=bool(beyond and beyond['item_fingerprint'] and beyond['item_fingerprint'] in (first['item_fingerprint'],last['item_fingerprint']))
                    verified=bool(terminal and not same and last['item_count'] and (empty or wrapped))
                    features['limits_and_wraparound']=dict(status='measured',beyond_response=br.get('number'),empty_candidate=empty,wraparound_or_clamp=wrapped,hidden_cap_absence_proven=False)
                    features['last_page']['verified']=verified;features['last_page']['end_evidence']='adjacent_empty_candidate' if empty else 'repeat_or_clamp' if wrapped else 'not_established'
                    s['cross_checks'].append(dict(method='adjacent_page_after_advertised_end',response=br.get('number'),conclusive=verified))
                    if verified and s['strategy']=='불가':s.update(strategy='마지막 페이지부터 거꾸로',confidence='보통')
                    suspect=suspect or same or wrapped or not verified
                if last.get('oldest_observed') and (oldest is None or last['oldest_observed'].get('date') and last['oldest_observed']['date']<(oldest.get('date') or '9999')):oldest=last['oldest_observed']
        # Repeat the source entry to distinguish an unstable key set from a bad pager.
        rr,repeated=read(url,'identity_stability_repeat',selectors)
        if repeated and first['item_fingerprint']:
            stable=first['item_fingerprint']==repeated['item_fingerprint']
            features['identity_stability'].update(status='measured',same_ordered_IDs_on_repeat=stable,response=rr['number'])
            suspect=suspect or not stable
            s['cross_checks'].append(dict(method='repeat_same_scope_identity',response=rr['number'],conclusive=stable))
        # Robots advertises another source method without guessing undocumented APIs.
        sitemap_links=list(first['sitemaps'])
        if suspect and not sitemap_links:
            robots_url=urlsplit(url)._replace(path='/robots.txt',query='',fragment='').geturl()
            rb,robots=read(robots_url,'robots_sitemap_discovery',{})
            if robots and robots.get('format')=='robots':sitemap_links=robots['sitemaps']
            features['independent_evidence']['robots_response']=rb.get('number')
            features['independent_evidence']['sitemap_links']=sitemap_links
        # An independently advertised sitemap is an alternative method, not a count for this scope.
        if suspect and sitemap_links:
            sr,sitemap=read(sitemap_links[0],'advertised_sitemap_cross_check',{})
            s['cross_checks'].append(dict(method='advertised_sitemap',response=sr.get('number'),conclusive=bool(sitemap and sitemap['items']),scope_equivalence_verified=False))
            features['independent_evidence']['sitemap_response']=sr.get('number')
        features['oldest_item']=dict(status='observed_sample' if oldest else 'unmeasurable',item=oldest,
            global_oldest_proven=False,reason=None if oldest else 'no_parseable_item_ID_and_date_in_bounded_responses')
        if s['strategy']=='불가':
            if first['date_filters']:s.update(strategy='연도 또는 날짜 구간 분할',confidence='낮음');s['blocked_reasons'].append('date_filter_execution_and_partition_coverage_unverified')
            elif first['sitemaps']:s.update(strategy='사이트맵',confidence='낮음');s['blocked_reasons'].append('sitemap_membership_equivalence_unverified')
            else:s['blocked_reasons'].append('no_verified_backfill_route_in_exposed_response')
        if not first['items']:s['blocked_reasons'].append('javascript_or_custom_list_contract_requires_review')
        if suspect:s['blocked_reasons'].append('suspicious_result_requires_independent_method_review')
        s['status']='needs_review' if suspect or s['confidence']=='낮음' else 'measured'
    if not result['scopes']:result['blocked_reasons']=['no_public_source_entry_available'];result['status']='unmeasurable'
    else:
        states=[s['status'] for s in result['scopes']]
        result['status']='needs_review' if 'needs_review' in states else 'unmeasurable' if all(s=='unmeasurable' for s in states) else 'measured'
    result['strategy']=list(dict.fromkeys(s['strategy'] for s in result['scopes'])) or ['불가']
    result['confidence']='보통' if result['status']=='measured' else '낮음'
    result['measured_tier']='보통' if result['status']=='measured' else '불가' if result['status']=='unmeasurable' else '불안정' if any('suspicious_result_requires_independent_method_review' in s['blocked_reasons'] for s in result['scopes']) else '낮음'
    result['finished_at']=utc_now();result['actual_HTTP_requests']=sum(r.get('status') is not None for r in result['requests'])
    return result


def summarize(out,manifest_data,base):
    results=[]
    for row in manifest_data['sites']:
        path=out/'sites'/(row['site_id']+'.json')
        if path.exists():results.append(json.loads(path.read_text()))
    counts=dict(Counter(r['status'] for r in results));counts['pending']=1245-len(results)
    summary=dict(updated_at=utc_now(),registered=1245,counts=counts,measured_tiers=dict(Counter(r['measured_tier'] for r in results)),
        original_tiers=dict(Counter(r['initial_tier'] for r in manifest_data['sites'])),
        requests=sum(r['actual_HTTP_requests'] for r in results),documents_saved=0,database_connections=0,
        goal_complete=False,completion_rule='1245 measured or explicit inability; needs_review remains unfinished')
    atomic_json(out/'summary.json',summary)
    stream=io.StringIO();writer=csv.DictWriter(stream,fieldnames=['site_id','initial_tier','measured_tier','status','strategy','confidence','requests','server','measured_at','blocked_reasons'])
    writer.writeheader()
    for r in results:writer.writerow(dict(site_id=r['site_id'],initial_tier=r['initial_tier'],measured_tier=r['measured_tier'],status=r['status'],strategy=';'.join(r['strategy']),confidence=r['confidence'],requests=r['actual_HTTP_requests'],server=r['server'],measured_at=r['finished_at'],blocked_reasons=';'.join(dict.fromkeys(reason for s in r['scopes'] for reason in s['blocked_reasons']))))
    tmp=out/'summary.csv.tmp';tmp.write_text(stream.getvalue());os.replace(tmp,out/'summary.csv')
    # Single runner owns these writes; all unrelated fields remain unchanged.
    pp=base/'progress.json';progress=json.loads(pp.read_text());progress['structure_discovery']={**summary,'directory':str(out.relative_to(base))};atomic_json(pp,progress)
    lp=base/'continuous-20261010/site-progress.json';ledger=json.loads(lp.read_text());by_id={r['site_id']:r for r in results}
    for row in ledger['sites']:
        if row['site_id'] in by_id:
            r=by_id[row['site_id']];row['structure_discovery']={k:r[k] for k in ('status','measured_tier','strategy','confidence','finished_at','actual_HTTP_requests')}
            row['structure_discovery']['evidence']=str((out/'sites'/(row['site_id']+'.json')).relative_to(base))
    ledger['structure_discovery_updated_at']=summary['updated_at'];atomic_json(lp,ledger)
    return summary


def main():
    p=argparse.ArgumentParser();p.add_argument('--base',type=Path,required=True);p.add_argument('--tiers',type=Path,default=DEFAULT_TIERS)
    p.add_argument('--workers',type=int,default=4);p.add_argument('--limit',type=int);p.add_argument('--site',action='append');p.add_argument('--budget',type=int,default=24)
    args=p.parse_args()
    if not 1<=args.workers<=8 or not 2<=args.budget<=30:raise ValueError('bounded workers and request budget required')
    out=args.base/'discovery-20261012';out.mkdir(parents=True,exist_ok=True)
    with (out/'runner.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        mp=out/'manifest.json'
        if not mp.exists():atomic_json(mp,manifest(args.tiers))
        data=json.loads(mp.read_text());transport=ReconTransport(out,args.base)
        pending=[r for r in data['sites'] if not (out/'sites'/(r['site_id']+'.json')).exists() and (not args.site or r['site_id'] in args.site)]
        if args.limit:pending=pending[:args.limit]
        state=dict(pid=os.getpid(),server=socket.gethostname(),started_at=utc_now(),status='running',scheduled=len(pending),finished=0,workers=args.workers,
                   explorer_sha256={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
                       ('delivery/scripts/discover_collection_structure.py','delivery/scripts/discovery_structure.py')})
        atomic_json(out/'runner.json',state)
        def run(row):
            if (out/'STOP').exists():return None
            try:return scout_site(row,transport,args.budget)
            except StopRequested:return None
            except Exception as exc:
                attempt_path=out/'attempts'/(row['site_id']+'.json')
                attempts=json.loads(attempt_path.read_text()).get('requests',[]) if attempt_path.exists() else []
                return dict(site_id=row['site_id'],initial_tier=row['initial_tier'],measured_tier='낮음',status='needs_review',
                    strategy=['불가'],confidence='낮음',server=socket.gethostname(),finished_at=utc_now(),actual_HTTP_requests=sum(r.get('status') is not None for r in attempts),requests=attempts,
                    scopes=[],error_type=type(exc).__name__,reason='runner_error_not_source_inability',documents_saved=0,database_connections=0)
        try:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures={pool.submit(run,row):row['site_id'] for row in pending}
                for future in as_completed(futures):
                    result=future.result()
                    if result is None:continue
                    atomic_json(out/'sites'/(result['site_id']+'.json'),result);state['finished']+=1;state['last_site']=result['site_id'];state['updated_at']=utc_now()
                    atomic_json(out/'runner.json',state)
                    if state['finished']%10==0:print(json.dumps(summarize(out,data,args.base)),flush=True)
            state['status']='stopped' if (out/'STOP').exists() else 'finished'
        finally:
            if state['status']=='running':state['status']='interrupted_or_failed'
            state['finished_at']=utc_now();atomic_json(out/'runner.json',state);print(json.dumps(summarize(out,data,args.base)),flush=True)


if __name__=='__main__':main()
