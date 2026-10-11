"""Review retained HAL count evidence; does not run another source census."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--evidence',required=True);ap.add_argument('--out',required=True);args=ap.parse_args()
    root=Path(args.evidence)
    ledger=json.loads((root/'site-progress.json').read_text())
    candidates=[]
    for site in ledger['sites']:
        if 'hal-science' not in site['site_id']:continue
        raw=site.get('source_count_candidates') or '[]'
        counts=json.loads(raw) if isinstance(raw,str) else raw
        for count in counts:
            if 4600000<=int(count.get('value',0))<=4800000:
                candidates.append({'site_id':site['site_id'],'unfiltered_candidate':count['value'],
                                   'reviewed_scoped_total':site.get('reviewed_query_reported_total')})
    f=root/'hal-enumeration-independent-review.json';review=json.loads(f.read_text())
    report={'conclusion':'469만은 기관별 독립 문서 수가 아니라 HAL 공통 인덱스 전체값을 여러 수집기에서 관측한 값이다. 사이트별 합산 금지.',
            'global_count_candidates_repeated_across_sites':candidates,
            'verified_subset':{'queries':review['completed_queries_reviewed'],
                              'query_memberships':review['query_memberships'],
                              'unique_docids':review['unique_docids_within_reviewed_query_union'],
                              'duplicate_memberships':review['query_memberships']-review['unique_docids_within_reviewed_query_union']},
            'all_hal_scopes_union_total_verified':False,
            'evidence':{'file':str(f),'sha256':hashlib.sha256(f.read_bytes()).hexdigest()},
            'qualification':'기관별 검색 범위도 서로 겹친다. 13개 범위의 중복 수를 전체 HAL 중복 수로 확대하지 않는다.'}
    Path(args.out).write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps({'repeated_global_candidates':len(candidates),'subset':report['verified_subset']},ensure_ascii=False))

if __name__=='__main__':main()
