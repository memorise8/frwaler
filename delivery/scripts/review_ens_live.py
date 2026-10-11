"""Independently compare all captured ENS memberships, details and stored metadata."""
import argparse
from collections import Counter
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sqlite3

from crawler.document_dates import normalize_date
from crawler.libertree_adapter import paper_to_document
from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json,utc_now
from delivery.scripts.measure_ens_incremental import request_kind


def run(root,source,out):
    root,source=Path(root),Path(source);audit=json.loads((root/'audit.json').read_text())
    assert audit['status']=='passed_full_metadata_scope_live_initial_resume_repeat';responses={}
    for r in audit['requests']:
        raw=(root/r['body']).read_bytes();assert r['status']==200 and hashlib.sha256(raw).hexdigest()==r['sha256']
        responses[r['url']]=raw.decode('utf-8')
    c=CRAWLERS['ens-lyon-fr-lecole'](None,delay=0);c._curl_get=lambda url,**kw:responses[url]
    conn=sqlite3.connect('file:'+str(root/'owned.sqlite')+'?mode=ro',uri=True);conn.row_factory=sqlite3.Row
    try:
        cards=[];page=0;last_date=None;duplicates=[];unique={}
        while True:
            data=c._ens_page(page);soup=c._make_soup(responses[c.LIST_URL+'?page='+str(page)])
            nodes=soup.select('.view-communiques-de-presse article')
            if not data['items']:break
            for item,node in zip(data['items'],nodes):
                date=c._node_text(node.select_one('.field--name-field-article-date'))
                date=normalize_date(date.removeprefix('Date de publication').strip(),order='DMY')['value']
                assert date and (last_date is None or date<=last_date);last_date=date
                item=dict(item,date=date)
                if item['key'] in unique:assert item==unique[item['key']];duplicates.append(item['key'])
                else:unique[item['key']]=item
                cards.append(item)
            if not data['has_next']:
                final=c._ens_page(page+1);assert not final['items'] and not final['has_next'];page+=1;break
            page+=1
        assert page==20 and len(unique)==239 and len(cards)==239 and not duplicates
        prior=json.loads((source/'ens-lyon-press-enumeration-independent-review.json').read_text());manifest=source/'ens-lyon-press-source-records.jsonl'
        assert hashlib.sha256(manifest.read_bytes()).hexdigest()==prior['source_manifest_sha256']
        old={r['post_number']:r['source_url'] for r in map(json.loads,manifest.read_text().splitlines())}
        assert all(k in unique and unique[k]['url']==url for k,url in old.items())
        added={k:i for k,i in unique.items() if k not in old}
        assert set(added)=={'593421'}
        rows=list(conn.execute('SELECT * FROM documents ORDER BY seq_id'));by_id={r['post_number']:r for r in rows};excluded={}
        assert len(rows)==238
        for key,item in unique.items():
            with contextlib.redirect_stdout(io.StringIO()):paper=c._parse_article(key,item['url'],responses[item['url']])
            assert not c.delivery_source_failure
            if paper is None:
                assert c._ens_last_exclusion and key not in by_id
                excluded[key]=c._ens_last_exclusion;continue
            row=by_id[key];stored=paper_to_document(paper)
            assert row['post_number']==stored['external_id'] and row['meta_url']==stored['meta_url']
            for name in ('title','abstract','pdf_url','original_filename','publisher','keywords','authors'):
                assert (row[name] or '')==(stored[name] or ''),(key,name)
            assert row['published_date_raw']==paper['published_date'] and row['listed_date_raw']==paper['posted_date']
            assert row['pdf_downloaded']==row['text_extracted']==0
        assert set(excluded)=={'1605424'}
        for phase in audit['phases']:
            assert phase['existing_rows_unchanged']
            assert {k:x['fingerprint'] for k,x in phase['state']['cursor']['exclusions'].items()}==excluded
        assert conn.execute('SELECT count(*) FROM crawl_incremental_pending').fetchone()[0]==0
        dates=[unique[r['post_number']]['date'] for r in rows];assert dates==sorted(dates)
        report=dict(status='passed',reviewed_at=utc_now(),listing_pages=20,adjacent_empty_page_verified=True,
                    source_memberships=len(cards),source_records=len(unique),stored_eligible_records=len(rows),excluded_source_records=excluded,
                    duplicate_source_memberships=duplicates,all_response_hashes_verified=True,response_count=len(audit['requests']),
                    request_kinds=dict(Counter(request_kind(r['url']) for r in audit['requests'])),
                    all_native_storage_fields_equal=True,existing_storage_normalizer='paper_to_document',
                    delivered_oldest_first=True,preserves_all_retained_original_ID_URLs=True,additional_source_records=added,
                    isolated_entire_metadata_scope_baseline_complete=True,global_source_complete=False,
                    full_files_verified=False,operating_database_touched=False,source_documents_deleted=False)
        atomic_json(Path(out),report);print(json.dumps(report));return report
    finally:c._session.close();conn.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--source',required=True);p.add_argument('--out',required=True)
    args=p.parse_args();run(args.root,args.source,args.out)
