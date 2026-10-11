"""Reconcile all retained ARCNL thesis lists and preserve native paper fields."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from crawler.incremental_arcnl import ARCNLAdapter, SCOPES, list_url
from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json, utc_now

SITE = 'arcnl-nl-publications'


def retained(source):
    source = Path(source)
    prior = json.loads((source/'eighteenth-arcnl97-scope-committed-review.json').read_text())
    followup = json.loads((source/'eighteenth-eight-followups-independent-review.json').read_text())
    c = CRAWLERS[SITE](None, delay=0); a = ARCNLAdapter(c)
    pages = {}; items = {}; groups = defaultdict(list); hashes = {}; actual = {}
    def checked(name, sha):
        raw = (source/name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != sha: raise ValueError('ARCNL retained source changed')
        hashes[name] = sha
        return raw
    try:
        for row in sorted(prior['source_responses'], key=lambda r: (r['category'], r['page'])):
            raw = checked(row['source_file'], row['source_sha256']); scope, page = row['category'], row['page']
            entries, last = a.inspect(raw.decode(), scope, page, empty=not row['memberships'])
            if len(entries) != row['memberships']: raise ValueError('ARCNL list count changed')
            url = list_url(scope, page); pages[url] = raw
            with patch.dict(c.crawl.__globals__, {'_curl_get': lambda u: raw.decode()}):
                if c._list_page_links(page, list_url(scope, 1)) != [i['url'] for i in entries]:
                    raise ValueError('native/adapter listing memberships differ')
            groups[scope] += entries
            for i in entries:
                if i['url'] in items: raise ValueError('retained thesis identity overlap changed')
                items[i['url']] = i
        proof = []
        for scope in SCOPES:
            dates = [i['listed_date'] for i in groups[scope]]
            if dates != sorted(dates, reverse=True) or len(dates) != prior['counts'][scope]:
                raise ValueError('ARCNL original scope order changed')
            proof.append(dict(scope=scope, count=len(dates), first=dates[0], last=dates[-1], missing=0,
                              increasing_pairs=0, decreasing_pairs=sum(x>y for x,y in zip(dates,dates[1:]))))
        for row in followup['rows']:
            if row['site_id'] != SITE or not row['scope'].endswith('first-detail'): continue
            raw = checked(row['source_file'], row['source_sha256']); url = row['url']; item = items[url]
            data = c._parse_detail(url, raw=raw.decode()); paper = c._paper(data, url, item['_scope'])
            expected = next(p for p in prior['after_replay']['papers'] if p['url'] == url)
            if paper != expected: raise ValueError('ARCNL actual native paper fields changed')
            if paper['published_date'] != item['listed_date']: raise ValueError('ARCNL source dates differ')
            actual[url] = dict(raw=raw, paper=paper, evidence=row)
        if len(items) != 97 or len(pages) != 14 or len(actual) != 3: raise ValueError('ARCNL scope changed')
        return pages, items, actual, dict(scope_order_proof=proof, source_hashes=hashes,
            retained_populated_pages=11, retained_adjacent_empty_pages=3, retained_unique_URLs=97,
            actual_native_papers_verified=3, all97_actual_details_verified=False)
    finally: c._session.close()


def review(source, probe):
    pages, items, actual, facts = retained(source); probe = Path(probe)
    audit = json.loads((probe/'audit.json').read_text()); c = CRAWLERS[SITE](None, delay=0); a = ARCNLAdapter(c)
    try:
        if audit['status'] != 'passed' or len(audit['requests']) != 3: raise ValueError('three current probes required')
        reordered = []
        for scope, row in zip(SCOPES, audit['requests']):
            raw = (probe/row['body']).read_bytes(); url = list_url(scope,1)
            if row['url'] != url or row['status'] != 200 or hashlib.sha256(raw).hexdigest() != row['sha256']:
                raise ValueError('ARCNL current source response changed')
            now, last = a.inspect(raw.decode(),scope,1)
            old, old_last = a.inspect(pages[url].decode(),scope,1)
            if (last != old_last or {i['url']:i for i in now} != {i['url']:i for i in old}
                    or [i['listed_date'] for i in now] != [i['listed_date'] for i in old]):
                raise ValueError('ARCNL current first-page metadata drift')
            if now != old: reordered.append(scope)
        root = Path(__file__).resolve().parents[2]
        deps = ['crawler/incremental_arcnl.py','delivery/health/access_challenge.py','crawler/document_dates.py']
        return dict(status='passed', site_id=SITE, reviewed_at=utc_now(), **facts,
            actual_live_HTTP_requests=3, current_three_first_page_membership_fields_match=True,
            same_date_internal_reordering_scopes=reordered,
            source_sha256=hashlib.sha256(Path(c.crawl.__code__.co_filename).read_bytes()).hexdigest(),
            dependency_source_sha256={p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in deps},
            current_probe_sha256=hashlib.sha256((probe/'audit.json').read_bytes()).hexdigest(),
            original_three_thesis_scopes_configured=True, short_native_abstract_requires_full_rescan=True,
            baseline_complete=False, global_source_complete=False, all_files_verified=False,
            operating_database_touched=False)
    finally:c._session.close()


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--probe',required=True);p.add_argument('--out',required=True)
    args=p.parse_args();out=Path(args.out)
    if out.exists(): raise FileExistsError(out)
    result=review(args.source,args.probe);atomic_json(out,result)
    print({k:result[k] for k in ('status','retained_unique_URLs','actual_native_papers_verified')})
