"""Apply narrowly reviewed endpoint strategies, preserving supplementary gaps.

These two queue entries explicitly request their native WordPress collection.
Failure to infer pagination from an additional landing page does not invalidate
the observed API boundary, but that landing-page limitation remains visible.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from delivery.health.results import atomic_json, utc_now
from delivery.scripts.live_check_structure import summarize


REVIEWS={
    'waltoninstitute-ie-research':dict(host='wordpress.waltoninstitute.ie',path='/wp-json/wp/v2/publication',
        filters={},page_size=100,cap=200,confidence='보통',
        scope_basis='Queue explicitly names the publication custom-post-type endpoint; collector PUBLICATION_API uses that same endpoint.',
        caveat='Oldest CMS date is 1970-01-01 and may be a placeholder. It is not a verified research publication date; timestamp ties are not unique.'),
    'ypergasias-gov-gr-category':dict(host='ypergasias.gov.gr',path='/wp-json/wp/v2/posts',
        filters={'categories':['4']},page_size=25,cap=200,confidence='높음',
        scope_basis='Queue explicitly requests categories=4; collector CATEGORY_ID and _list_api_url use the same category filter.',
        caveat='WordPress date denotes the CMS post date. Other categories and the unfiltered posts endpoint are outside this selected strategy.'),
}


def resolve(base):
    out=base/'live-check-20261012';archive=out/'before-strategy-review';archive.mkdir(exist_ok=True)
    data=json.loads((out/'plans.json').read_text());decisions=[]
    for site,review in REVIEWS.items():
        path=out/'sites'/(site+'.json');result=json.loads(path.read_text())
        candidates=[]
        for scope in result['scopes']:
            p=urlsplit(scope['url']);query=parse_qs(p.query)
            if p.hostname!=review['host'] or p.path!=review['path'] or scope['status']!='confirmed':continue
            if any(query.get(k)!=v for k,v in review['filters'].items()):continue
            candidates.append(scope)
        if not candidates:raise ValueError('reviewed endpoint has no confirmed boundary: '+site)
        chosen=candidates[0];checks=chosen['checks']
        if checks['last_and_after']['status']!='verified' or checks['total_arithmetic']['status']!='verified' or checks['oldest_sort_or_date_filter']['status']!='verified':
            raise ValueError('required native metadata checks incomplete: '+site)
        source=Path(result['source_file']);source_sha=hashlib.sha256(source.read_bytes()).hexdigest()
        if source_sha!=result['source_sha256']:raise ValueError('collector source changed since measurement')
        original=archive/(site+'.json')
        if not original.exists():original.write_bytes(path.read_bytes())
        count=checks['total_arithmetic']['total'];required_pages=math.ceil(count/review['page_size'])
        decision=dict(reviewed_at=utc_now(),status='reviewed_requested_native_collection',
            live_check=result['live_check'],selected_scope=chosen['url'],strategy='오래된 순 정렬',
            confidence=review['confidence'],scope_basis=review['scope_basis'],caveat=review['caveat'],
            total=count,oldest_observed=checks['oldest_item']['item'],
            collector_page_size=review['page_size'],collector_default_page_cap=review['cap'],
            collector_pages_needed=required_pages,cap_smaller_than_current_scope=required_pages>review['cap'],
            source_file=str(source),source_sha256=source_sha,
            original_record=str(original),all_documents_collected=False,
            supplementary_unconfirmed_scopes=[s['url'] for s in result['scopes'] if s['status']!='confirmed'])
        result.update(status='confirmed',strategy=decision['strategy'],confidence=decision['confidence'],
            reason='requested_native_collection_strategy_reviewed',strategy_review=decision)
        atomic_json(path,result);decisions.append(dict(site_id=site,**decision))
    atomic_json(out/'strategy-decisions.json',dict(updated_at=utc_now(),sites=decisions,
        limitation='Only the explicitly requested native collections are resolved here. Remaining sites still require their own disposition.'))
    return summarize(out,base,data)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--base',required=True,type=Path)
    args=parser.parse_args();print(json.dumps(resolve(args.base),ensure_ascii=False))


if __name__=='__main__':main()
