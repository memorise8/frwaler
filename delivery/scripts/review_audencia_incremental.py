"""Replay the retained complete Audencia catalogue and explicit empty boundary."""
import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from crawler.incremental import infer_order,observe_order
from crawler.incremental_audencia import AudenciaAdapter
from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json,utc_now


def run(source,out):
    source=Path(source); prior=json.loads((source/'audencia-all21-catalogue-independent-review.json').read_text())
    manifest=source/prior['records_file'];assert hashlib.sha256(manifest.read_bytes()).hexdigest()==prior['records_sha256']
    expected=[json.loads(s) for s in manifest.read_text().splitlines()]
    c=CRAWLERS['audencia-com-lecole'](None,delay=0);responses={}
    try:
        for p in prior['pages']:
            raw=(source/p['source_file']).read_bytes();assert hashlib.sha256(raw).hexdigest()==p['source_sha256']
            responses[c._list_url(p['page'])]=raw.decode('utf-8')
        c._request=lambda url,**kw:SimpleNamespace(text=responses[url]);a=AudenciaAdapter(c)
        entries=[];cursor=None;proof={}
        while True:
            page=a.fetch(cursor,'native');entries.extend(page.entries);observe_order(proof,page.entries,False);cursor=page.next_cursor
            if page.exhausted:break
        assert len(entries)==len({e.key for e in entries})==246
        by_url={r['url']:r for r in expected};assert {e.key for e in entries}==set(by_url)
        fields={'title':'title_raw','listed_date_raw':'listed_date_text_raw','listed_datetime':'listed_datetime_raw','category':'category_raw'}
        for e in entries:assert all(e.payload[k]==by_url[e.key][v] for k,v in fields.items())
        assert infer_order(proof)==('newest_first','date')
        report=dict(status='passed',reviewed_at=utc_now(),retained_pages=21,adjacent_empty_page_verified=True,
                    records=246,all_response_hashes_verified=True,all_native_list_fields_equal=True,
                    manifest_sha256=prior['records_sha256'],merged_order=infer_order(proof),proof=proof,
                    discovery_identity='original_detail_URL',storage_identity='native_Drupal_node_ID_or_slug',
                    live_http_requests=0,all_detail_source_IDs_verified=False,global_source_complete=False)
        atomic_json(Path(out),report);print(json.dumps(report));return report
    finally:c._session.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--out',required=True)
    args=p.parse_args();run(args.source,args.out)
