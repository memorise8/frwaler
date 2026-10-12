"""Finish concrete queue URL observations before semantic batch disposition.

No traversal is invented here: each request names an unobserved compiled URL,
or refreshes a requested sitemap for missing scoped-count metadata. All prior
requests count toward the same 30-request ceiling. Native REST projection is
explicit, and raw listing bodies are never saved.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor,as_completed
import fcntl
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import parse_qsl,urlsplit,urlunsplit,urlencode

from delivery.health.results import atomic_json,utc_now
from delivery.scripts.audit_live_structure import normalized
from delivery.scripts.live_check_structure import prepare,summarize
from delivery.scripts.live_check_probes import ProbeSession
from delivery.scripts.live_check_transport import LiveTransport,AccessStopped,BudgetReached,LiveStop
from delivery.scripts.probe_wordpress_structure import FIELDS


def metadata_url(url):
    p=urlsplit(url)
    if '/wp-json/wp/v2/' in p.path:
        pairs=[(k,v) for k,v in parse_qsl(p.query,keep_blank_values=True) if k!='_fields']
        pairs.append(('_fields',FIELDS))
        return urlunsplit(p._replace(query=urlencode(pairs)))
    return url


def process(plan,out,transport):
    path=out/'sites'/(plan['site_id']+'.json');result=json.loads(path.read_text())
    if result.get('URL_closure',{}).get('complete'):return result
    if result['status']=='access_blocked':return result
    attempt=out/'attempts'/(plan['site_id']+'.json')
    if attempt.exists():
        saved=json.loads(attempt.read_text())
        if len(saved.get('requests',[]))>len(result['requests']):result=saved
    archive=out/'before-url-closure';archive.mkdir(exist_ok=True)
    if not (archive/path.name).exists():(archive/path.name).write_bytes(path.read_bytes())
    def persist():atomic_json(attempt,result)
    session=ProbeSession(plan,transport,result,persist,seed_summaries=True)
    seen={normalized(r['url']) for r in result['requests']}
    closure=dict(started_at=utc_now(),observations=[],complete=False)
    result['URL_closure']=closure
    hosts={urlsplit(u).hostname for u in plan['original_scope_urls']}
    hosts.update(urlsplit(s['checks']['domain_migration']['resolved_url']).hostname for s in result['scopes'] if s['checks']['domain_migration'].get('resolved_url'))
    with transport.lock:holds={h:transport.held[h] for h in hosts if h in transport.held}
    if holds:
        closure.update(complete=True,finished_at=utc_now(),stopped_reason='existing_source_hold_preserved',hold_evidence=holds)
        atomic_json(path,result);persist();return result
    for group in plan['targets']:
        for target in group:
            url=target['url'];actual=metadata_url(url)
            if normalized(url) in seen or normalized(actual) in seen:continue
            if target['provenance']=='original_registered_scope':continue
            if result['status']=='confirmed' and '/wp-json/' in url:
                closure['observations'].append(dict(url=url,status='covered_by_reviewed_native_metadata_projection'));continue
            try:
                response,raw,redirects=session.entry(actual)
                closure['observations'].append(dict(url=url,actual_URL=actual,response=response['number'],
                    status='observed' if response.get('status') else 'unavailable_transport',redirects=redirects))
                seen.add(normalized(actual));persist()
            except (AccessStopped,BudgetReached,ValueError) as exc:
                closure['observations'].append(dict(url=url,status=type(exc).__name__,reason=str(exc)))
                persist()
                if isinstance(exc,(AccessStopped,BudgetReached)):
                    closure['stopped_reason']=str(exc);break
        if closure.get('stopped_reason'):break
    # The DHS instruction explicitly asks for the union across 1..14. Page 1
    # was measured before prefix metadata existed; refresh only that missing
    # summary, not a detail page or previously completed network traversal.
    if plan['site_id']=='dhs-gov-congressional-approp' and not closure.get('stopped_reason'):
        url='https://www.dhs.gov/sitemap.xml?page=1'
        try:session.read(url,'refresh_requested_sitemap_prefix_count',repeat=True)
        except (AccessStopped,BudgetReached,ValueError) as exc:closure['sitemap_refresh_unavailable']=str(exc)
    by_prefix={}
    for response in result['requests']:
        for count in response.get('summary',{}).get('requested_prefix_counts',[]):
            by_prefix.setdefault(count['prefix'],{})[response['url']]=count
    closure['scoped_sitemap_observations']=[dict(prefix=prefix,observed_URLs=sorted(rows),
        unique_URL_count=len(set().union(*(set(c['unique_URL_hashes']) for c in rows.values()))) if all(c['hashes_complete'] for c in rows.values()) else None,
        complete_only_for_listed_URLs=True,source_wide_coverage_proven=False) for prefix,rows in by_prefix.items()]
    closure.update(complete=True,finished_at=utc_now())
    result.update(actual_HTTP_responses=sum(r.get('status') is not None for r in result['requests']),finished_at=utc_now())
    atomic_json(path,result);persist();return result


def run(base,batch='primary'):
    out=base/('live-check-20261012' if batch=='primary' else 'live-check-extra-20261012')
    with (out/'runner.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with tempfile.TemporaryDirectory() as tmp:data=prepare(Path(tmp),base/'discovery-20261012',out/'input-queue.json',batch=batch)
        if any(not (out/'sites'/(p['site_id']+'.json')).exists() for p in data['sites']):
            raise ValueError('finish the initial pass before closing remaining explicit URLs')
        before=out/'plans.before-url-closure.json'
        if not before.exists():before.write_bytes((out/'plans.json').read_bytes())
        atomic_json(out/'plans.json',data);transport=LiveTransport(out,base/'discovery-20261012')
        state=dict(pid=os.getpid(),status='running',started_at=utc_now(),finished=0)
        atomic_json(out/'url-closure-runner.json',state)
        try:
            for delivery in (True,False):
                with ThreadPoolExecutor(max_workers=8) as pool:
                    for future in as_completed([pool.submit(process,p,out,transport) for p in data['sites'] if p['delivery']==delivery]):
                        future.result();state['finished']+=1
                        if state['finished']%25==0:atomic_json(out/'url-closure-runner.json',state)
            state['status']='finished'
        finally:
            if state['status']=='running':state['status']='interrupted_or_failed'
            state['finished_at']=utc_now();atomic_json(out/'url-closure-runner.json',state)
            print(json.dumps(summarize(out,base,data),ensure_ascii=False),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--base',required=True,type=Path)
    parser.add_argument('--batch',choices=('primary','extra'),default='primary')
    args=parser.parse_args();run(args.base,args.batch)
