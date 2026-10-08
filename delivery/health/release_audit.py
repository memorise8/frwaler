"""Offline delivery capability inventory. Does not fetch sites or open a DB."""
import argparse
import csv
import json
from collections import Counter
from pathlib import Path

from crawler.sites import CRAWLERS, REGISTRY_ERRORS
from delivery.capabilities import capabilities
from .inventory import inventory
from .results import utc_now


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    catalogue=inventory()
    ids={r['site_id'] for r in catalogue['sites']}
    rows=[]
    for site in sorted(CRAWLERS):
        data=capabilities(site)
        rows.append({'site_id':site,'in_delivery_catalogue':site in ids,
                     'order':data['order'],'count_semantics':data['count_semantics'],
                     'resume_signals_only':data['resume_signals'],
                     'chronological_resume':data['features']['chronological_resume'],
                     'incremental':data['modes']['incremental']['support'],
                     'full':data['modes']['full']['support'],
                     'backfill':data['modes']['backfill']['support'],
                     'key_name':data['credential']['name'], 'key_required':data['credential']['required'],
                     'verification':data['verification']['level'], 'live_verification':'not_verified',
                     'source_hash':data['source_hash']})
    args.out.mkdir(parents=True,exist_ok=True)
    with (args.out/'capabilities.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]),lineterminator="\n");writer.writeheader();writer.writerows(rows)
    summary={'checked_at':utc_now(),'scope':'offline_code_only','total':len(rows),'catalogue':len(ids),
             'extra_registry':len(rows)-len(ids),'registry_errors':REGISTRY_ERRORS,
             'count_semantics':dict(Counter(r['count_semantics'] for r in rows)),
             'chronological_resume_supported':0,'live_verified':0,
             'note':'Static signals and fixture tests do not certify complete/live site collection.'}
    (args.out/'audit.json').write_text(json.dumps({'summary':summary,'sites':rows},ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(summary,ensure_ascii=False))


if __name__=='__main__':main()
