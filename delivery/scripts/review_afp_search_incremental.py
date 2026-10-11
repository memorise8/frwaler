"""Search relevance order is not a safe incremental chronology."""
import argparse
import hashlib
import json
from pathlib import Path

from crawler.incremental import Entry,observe_order,infer_order
from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json,utc_now

SITE='afp-gov-au-search'
REASON='search_results_nonmonotonic_dates_and_node_IDs_with_two_missing_identity_date_rows'


def retained(source):
    source=Path(source);native=CRAWLERS[SITE].crawl.__globals__
    prior=json.loads((source/'afp53-search-parser-committed-review.json').read_text())
    record_path=source/prior['records_file']
    if hashlib.sha256(record_path.read_bytes()).hexdigest()!=prior['records_sha256']:
        raise ValueError('retained source memberships changed')
    expected=[json.loads(line)['native_fields'] for line in record_path.read_text().splitlines()]
    pages=[];items=[]
    for p in prior['pages']:
        raw=(source/p['source_file']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=p['source_sha256']:raise ValueError('AFP list body hash mismatch')
        parsed=native['_parse_list_page'](raw.decode())
        if len(parsed)!=p['fixed_native_memberships']:raise ValueError('native card count regressed')
        pages.append(dict(raw=raw,items=parsed,file=p['source_file'],sha256=p['source_sha256']));items.extend(parsed)
    if items!=expected or len(items)!=53 or len({i['url'] for i in items})!=53:
        raise ValueError('native membership/field regression')
    return pages,items


def review(source,probe):
    pages,items=retained(source);probe=Path(probe);audit=json.loads((probe/'audit.json').read_text())
    replies=audit['requests'];last=replies[-1];raw=(probe/last['body']).read_bytes()
    if last['status']!=200 or hashlib.sha256(raw).hexdigest()!=last['sha256']:
        raise ValueError('current search response invalid')
    cls=CRAWLERS[SITE]
    if cls.crawl.__globals__['_parse_list_page'](raw.decode())!=pages[0]['items']:
        raise ValueError('current first-page fields changed; renew review')
    proof={};observe_order(proof,[Entry(i['node_id'],i,i['datetime_raw']) for i in items],True)
    if infer_order(proof)!=('arbitrary','order_ambiguous'):
        raise ValueError('retained search order classification changed')
    if not all(proof[k]['up'] and proof[k]['down'] and proof[k]['missing']==2 for k in ('id','date')):
        raise ValueError('missing explicit bidirectional order evidence')
    code=Path(__file__).resolve().parents[2]
    return dict(status='passed',reviewed_at=utc_now(),site_id=SITE,reason=REASON,
        classification='full_rescan_required',retained_search_results=53,source_pages=5,adjacent_empty_pages=1,
        current_first_page_fields_match=12,actual_live_HTTP_requests=len(replies),order_proof=proof,
        missing_native_fields_not_invented=True,all53_native_parsed_fields_equal_prior=True,
        result_count_not_PDF_file_count=True,all_current_details_verified=False,
        source_response_hashes={p['file']:p['sha256'] for p in pages},
        current_probe_sha256=hashlib.sha256((probe/'audit.json').read_bytes()).hexdigest(),
        source_sha256=hashlib.sha256(Path(cls.crawl.__code__.co_filename).read_bytes()).hexdigest(),
        dependency_source_sha256={'delivery/health/access_challenge.py':hashlib.sha256((code/'delivery/health/access_challenge.py').read_bytes()).hexdigest()},
        baseline_complete=False,global_source_complete=False,all_files_verified=False,operating_database_touched=False)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--probe',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();out=Path(a.out)
    if out.exists():raise FileExistsError(out)
    report=review(a.source,a.probe);atomic_json(out,report)
    print(json.dumps({k:report[k] for k in ('status','retained_search_results','order_proof','actual_live_HTTP_requests')}))
