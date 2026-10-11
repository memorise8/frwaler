"""Reconcile BMLEH order evidence without inventing days for month-only labels."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path

from crawler.incremental import Entry,observe_order,infer_order
from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json,utc_now


def advisory_order(items):
    helpers=CRAWLERS['bmleh-de-en']._build_document.__globals__
    periods=[];precisions=[];examples=[]
    for item in items:
        matches=[m for m in helpers['_DATE_RE'].finditer(item['trailing'])
                 if m.group('month').lower() in helpers['_MONTHS']]
        if not matches:
            periods.append(None);precisions.append('unknown');continue
        m=matches[-1];period=(int(m.group('year')),helpers['_MONTHS'][m.group('month').lower()])
        periods.append(period);precisions.append('day' if m.group('day') else 'month')
    for i,(before,after) in enumerate(zip(periods,periods[1:]),1):
        if before and after and after>before:
            examples.append({'before_index':i-1,'after_index':i,
                             'before_raw':items[i-1]['trailing'],'after_raw':items[i]['trailing'],
                             'before_period':before,'after_period':after})
    return {'items':len(items),'month_precision':precisions.count('month'),
            'day_precision':precisions.count('day'),'unknown_precision':precisions.count('unknown'),
            'month_order_inversions':examples,'periods_are_not_synthetic_full_dates':True}


def run(source,probe,out):
    source=Path(source);probe=Path(probe);out=Path(out)
    prior_path=source/'bmleh-full-history-independent-review.json';prior=json.loads(prior_path.read_text())
    manifest=source/'bmleh-full-history-source-memberships.jsonl'
    assert hashlib.sha256(manifest.read_bytes()).hexdigest()==prior['files'][manifest.name]
    rows=[json.loads(x) for x in manifest.open()];groups=defaultdict(list);files={}
    for r in rows:groups[r['scope']].append(r);files[r['source_file']]=r['source_sha256']
    for name,digest in files.items():assert hashlib.sha256((source/name).read_bytes()).hexdigest()==digest
    c=CRAWLERS['bmleh-de-en'](None,delay=0);advisory=groups['scope-0']
    original=c._parse_list((source/advisory[0]['source_file']).read_text())
    assert [(x['href'],x['trailing']) for x in original]==[(x['url'],x['trailing_date_and_version_text_raw']) for x in advisory]
    fresh=json.loads(probe.read_text());req=fresh['requests'][0];body=probe.parent/req['body']
    assert req['status']==200 and hashlib.sha256(body.read_bytes()).hexdigest()==req['sha256']
    current=c._parse_list(body.read_text());old_order=advisory_order(original);new_order=advisory_order(current)
    assert old_order==new_order and len(new_order['month_order_inversions'])>=2
    assert [x['href'] for x in original]==[x['href'] for x in current]
    proofs={}
    for scope,records in groups.items():
        if scope=='scope-0':continue
        proof={};observe_order(proof,[Entry(r['url'],{},r.get('listed_datetime_raw') or '') for r in records],False)
        proofs[scope]={'items':len(records),'proof':proof,'inferred_order':infer_order(proof)}
    report={'status':'passed','reviewed_at':utc_now(),'site_id':c.site_id,
            'classification':'full_rescan_required',
            'reason':'advisory_month_precision_and_nonchronological_versions_prevent_whole_scope_early_stop',
            'source_sha256':hashlib.sha256(Path(c.crawl.__code__.co_filename).read_bytes()).hexdigest(),
            'manifest_sha256':hashlib.sha256(manifest.read_bytes()).hexdigest(),
            'retained_source_files_verified':len(files),'retained_memberships':len(rows),
            'retained_unique_urls':len({r['url'] for r in rows}),'advisory_order':new_order,
            'chronological_other_scopes':proofs,'fresh_requests':1,'fresh_advisory_urls_equal_retained':True,
            'legacy_only_configures_advisory':True,'global_source_complete':False,
            'note':'Do not turn month-only labels into a first-of-month boundary. Chronological press/law/archive scopes do not certify the advisory scope.'}
    atomic_json(out,report);print(json.dumps({k:v for k,v in report.items() if k not in ('chronological_other_scopes','source_sha256','manifest_sha256')}))
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--probe',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();run(a.source,a.probe,a.out)
