"""Export all registered collectors without making source requests."""
import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from delivery.incremental_status import inventory


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--out',required=True);args=ap.parse_args()
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    rows=inventory()
    for name,values in [('all-sites',rows),('full-rescan-required',[r for r in rows if r['classification']=='full_rescan_required'])]:
        (out/(name+'.json')).write_text(json.dumps(values,ensure_ascii=False,indent=2))
        with (out/(name+'.csv')).open('w',newline='',encoding='utf-8-sig') as f:
            writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(values)
    summary={'registered':len(rows),'classifications':dict(Counter(r['classification'] for r in rows)),
             'adapters':dict(Counter(r['adapter'] or 'none' for r in rows)),
             'old_arbitrary':sum(r['declared_order_before']=='arbitrary' for r in rows),
             'all_old_arbitrary_have_explicit_classification':all(r['classification'] for r in rows if r['declared_order_before']=='arbitrary'),
             'all_order_directions_resolved':False}
    (out/'summary.json').write_text(json.dumps(summary,indent=2));print(json.dumps(summary))

if __name__=='__main__':main()
