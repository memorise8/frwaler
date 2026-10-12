"""Robots-aware bounded read transport; persistent refusals and one 429 retry."""
import hashlib
import json
from pathlib import Path
import threading
import time
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from delivery.health.results import atomic_json, utc_now
from delivery.health.request_gate import RequestGate
from delivery.scripts.discover_collection_structure import ReconTransport


class BudgetReached(Exception): pass
class AccessStopped(Exception): pass
class LiveStop(Exception): pass


class LiveTransport(ReconTransport):
    def __init__(self, out, prior):
        super().__init__(out, Path(out).parent)
        self.prior=Path(prior);self.robots={};self.robots_locks={};self.retry_used=set()
        state=self.out/'rate-retries.json'
        if state.exists(): self.retry_used=set(json.loads(state.read_text()))
        # Prior restrictions apply to this stage as well. Do not switch routes,
        # hosts or clients in response to a source refusal.
        if (self.prior/'access-holds.json').exists():
            previous=json.loads((self.prior/'access-holds.json').read_text())
            self.held={**previous,**self.held};atomic_json(self.out/'access-holds.json',self.held)

    def gate(self, host):
        with self.lock:
            if host not in self.gates:
                self.gates[host]=RequestGate(self.prior/'request-gate'/hashlib.sha256(host.encode()).hexdigest()[:20],delay=1.5)
            return self.gates[host]

    def single(self, url, purpose, record, error_json=False):
        if (self.out/'STOP').exists(): raise LiveStop()
        record.reserve()  # budget check BEFORE any socket, including robots/retry
        response,raw=super().fetch(url,inspect_error_json=error_json)
        response['purpose']=purpose;record(response)
        return response,raw

    def fetch_checked(self, url, purpose, record, error_json=False):
        host=urlsplit(url).hostname
        response,raw=self.single(url,purpose,record,error_json)
        if response.get('status')!=429 and not (response.get('error')=='host_access_hold' and response.get('hold_evidence',{}).get('reason')=='rate_limited'):
            return response,raw
        gate=self.gate(host)
        retry_at=max(gate._read().get('cooldowns',{}).get(host,0),time.time()+3)
        response['single_retry_at']=retry_at
        # Keep the job explicitly deferred when a publisher requests a long wait.
        if retry_at-time.time()>180:
            response['retry_deferred_until']=retry_at;return response,None
        record.reserve()  # preserve the refusal when no retry budget remains
        with self.lock:
            if host in self.retry_used: return response,None
            self.retry_used.add(host);atomic_json(self.out/'rate-retries.json',sorted(self.retry_used))
        while time.time()<retry_at:
            if (self.out/'STOP').exists(): raise LiveStop()
            time.sleep(max(0,min(1,retry_at-time.time())))
        with self.lock:
            held=self.held.get(host)
            if held and held.get('reason')=='rate_limited':
                self.held.pop(host);atomic_json(self.out/'access-holds.json',self.held)
        gate.delay=max(gate.delay,3.0)
        return self.single(url,purpose+'_one_rate_limit_retry',record,error_json)

    def allowed(self, url, record):
        parts=urlsplit(url);host=parts.hostname
        with self.lock:
            lock=self.robots_locks.setdefault(host,threading.Lock())
            hold=self.held.get(host)
        if hold and hold.get('reason')!='rate_limited':
            raise AccessStopped('prior_access_hold:'+hold['reason'])
        with lock:
            if host not in self.robots:
                robots_url=f'{parts.scheme}://{parts.netloc}/robots.txt'
                response,raw=self.fetch_checked(robots_url,'robots_policy',record)
                for _ in range(2):
                    target=response.get('redirect')
                    if not target: break
                    if urlsplit(target).hostname!=host:
                        self.robots[host]=(None,dict(status='unconfirmable',reason='cross_host_robots_policy_requires_review',response=response));break
                    response,raw=self.fetch_checked(target,'robots_policy_redirect',record)
                else: pass
                if host not in self.robots:
                    if response.get('status') in (404,410):
                        self.robots[host]=(None,dict(status='not_published',response=response))
                    elif raw is not None and response.get('status')==200:
                        text=raw.decode('utf-8',errors='replace')
                        if '<html' in text[:500].lower() or '<!doctype html' in text[:500].lower():
                            self.robots[host]=(None,dict(status='unconfirmable',reason='robots_HTML_instead_of_policy',response=response))
                        else:
                            policy=RobotFileParser();policy.set_url(robots_url);policy.parse(text.splitlines())
                            self.robots[host]=(policy,dict(status='parsed',response=response))
                    else:self.robots[host]=(None,dict(status='unavailable',response=response))
            policy,evidence=self.robots[host]
        if evidence['status'] not in ('parsed','not_published'):
            raise AccessStopped('robots_policy_unavailable:'+str(evidence.get('reason') or evidence['response'].get('error') or evidence['response'].get('status')))
        if policy and not policy.can_fetch('Libertree-Structure-Recon',url):
            raise AccessStopped('robots_disallow_for_requested_path')
        if policy:
            delay=policy.crawl_delay('Libertree-Structure-Recon') or policy.crawl_delay('*')
            if delay and delay>10: raise AccessStopped('robots_crawl_delay_above_shared_gate_limit:'+str(delay))
            if delay:self.gate(host).delay=max(self.gate(host).delay,float(delay))
        return dict(status=evidence['status'],robots_url=evidence['response']['url'],
                    robots_sha256=evidence['response'].get('sha256'),checked_at=utc_now())
