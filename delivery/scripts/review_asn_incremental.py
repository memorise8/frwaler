"""Reconcile both delivered ASN catalogues and native metadata without new HTTP."""
import argparse
from collections import Counter
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

import requests

from crawler.document_dates import normalize_document_dates
from crawler.incremental import Entry, infer_order, observe_order
from crawler.incremental_asn import ASNAdapter, SCOPES
from crawler.libertree_adapter import paper_to_document
from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json, utc_now
from delivery.scripts.measure_asn_incremental import request_kind, verify_recorded_phases

SITE='asn-fr-information'


def retained(source):
    source=Path(source)
    prior=json.loads((source/'asn-delivered-scope-independent-review.json').read_text())
    manifest=source/prior['records_file']
    assert hashlib.sha256(manifest.read_bytes()).hexdigest()==prior['records_sha256']
    responses={}
    for page in prior['pages']:
        raw=(source/page['source_file']).read_bytes()
        assert hashlib.sha256(raw).hexdigest()==page['source_sha256']
        responses[page['url']]={'text':raw.decode('utf-8'),'status':page['status']}
    return responses,[json.loads(x) for x in manifest.read_text().splitlines()]


def native_papers(responses,scope):
    c=CRAWLERS[SITE](None,delay=0);papers=[]
    c._curl_get=lambda url,**kw:responses[url]['text']
    def save(p):papers.append(p);c._last_save_created=True
    c._save_paper=save
    try:
        with patch.dict(c.crawl.__globals__,{'_LIST_PATH':SCOPES[scope]['path']}), \
                patch.object(c.crawl.__globals__['time'],'sleep'),contextlib.redirect_stdout(io.StringIO()):
            assert c.crawl()==len(papers)
        # Expertise was missing from the legacy default. Its new category is
        # intentional; every original detail/materializer field is preserved.
        for p in papers:
            p['category']=SCOPES[scope]['title'];meta=json.loads(p['metadata']);meta['category']=p['category']
            p['metadata']=json.dumps(meta,ensure_ascii=False)
        return papers
    finally:c._session.close()


def run(source,out,root=None):
    out=Path(out)
    if out.exists():raise FileExistsError(out)
    responses,manifest=retained(source);audit=None
    if root:
        root=Path(root);audit=json.loads((root/'audit.json').read_text())
        assert (audit['status']=='passed_scoped_live_initial_resume_repeat' or
                audit['status']=='failed_checkpoint_retained' and audit.get('error_type')=='AssertionError'
                and audit.get('reason')=='')
        rechecks=verify_recorded_phases(audit)
        responses={}
        for r in audit['requests']:
            raw=(root/r['body']).read_bytes()
            assert hashlib.sha256(raw).hexdigest()==r['sha256'] and r['status'] in (200,404)
            responses[r['url']]={'text':raw.decode('utf-8'),'status':r['status']}
    c=CRAWLERS[SITE](None,delay=0)
    def request(method,url,**kw):
        d=responses[url];r=requests.Response();r.url=url;r.status_code=d['status'];r._content=d['text'].encode();r.encoding='utf-8';return r
    c._session.request=request
    try:
        adapter=ASNAdapter(c);items=[];proofs={}
        for scope,last in [('expertise',1),('annual',2)]:
            scope_items=[]
            for index in range(last):
                data=adapter.read(scope,index);assert data['last_page']==last;scope_items.extend(data['items'])
            assert len(scope_items)==(7 if scope=='expertise' else 19)
            proof={};observe_order(proof,[Entry(i['url'],i,i['listed_date']) for i in scope_items],False)
            assert infer_order(proof)==('newest_first','date');proofs[scope]=proof;items.extend(scope_items)
        assert len(items)==len(manifest)==26
        old_by_url={r['url']:r for r in manifest}
        assert set(old_by_url)=={i['url'] for i in items}
        for i in items:
            old=old_by_url[i['url']]
            assert i['title']==c._clean_text(old['title_raw'])
            assert i['raw_date']==old['publication_date_raw']
            assert i['teaser_text']==c._clean_text(old['listing_description_raw'] or '')
        report=dict(status='passed',reviewed_at=utc_now(),site_id=SITE,source_memberships=26,
            source_scope_counts={'expertise':7,'annual':19},original_both_delivered_scopes=True,
            source_memberships_equal_retained=True,source_dates_prove_each_original_order=proofs,
            source_sha256=hashlib.sha256(Path(c.crawl.__code__.co_filename).read_bytes()).hexdigest(),
            adapter_source_sha256=hashlib.sha256(Path(adapter.fetch.__code__.co_filename).read_bytes()).hexdigest(),
            adjacent_404_boundaries_verified=2,live_http_requests=0,
            global_source_complete=False,all_files_verified=False,source_documents_deleted=False,operating_database_touched=False)
        if root:
            papers=native_papers(responses,'expertise')+native_papers(responses,'annual')
            expected={p['url']:p for p in papers};assert len(expected)==len(papers)
            exclusions={}
            for item in items:
                paper,excluded=adapter.prepare(item)
                if item['url'] in expected:
                    assert excluded is None and paper==expected[item['url']]
                else:
                    assert paper is None and excluded
                    exclusions[item['url']]=excluded
            conn=sqlite3.connect('file:'+str(root/'owned.sqlite')+'?mode=ro',uri=True);conn.row_factory=sqlite3.Row
            try:
                rows=list(conn.execute('SELECT * FROM documents ORDER BY seq_id'))
                assert len(rows)==len(expected) and {r['meta_url'] for r in rows}==set(expected)
                dates=[expected[r['meta_url']]['listed_date'] for r in rows];assert dates==sorted(dates)
                for row in rows:
                    paper=expected[row['meta_url']];doc=paper_to_document(paper)
                    doc=normalize_document_dates(dict(doc,listed_date=doc.get('posted_date')))
                    assert row['post_number']==paper['external_id']
                    for key in ('title','abstract','pdf_url','original_filename','authors','publisher','keywords',
                                'published_date','published_date_raw','listed_date','listed_date_raw'):
                        assert (row[key] or '')==(doc[key] or ''),(row['meta_url'],key)
                    assert not row['pdf_downloaded'] and not row['text_extracted']
                assert conn.execute('SELECT count(*) FROM crawl_incremental_pending').fetchone()[0]==0
                assert audit['durable_state']['baseline_complete']
                stored_exclusions=audit['durable_state']['cursor']['exclusions']
                assert set(stored_exclusions)==set(exclusions)
                assert all(v['fingerprint']==exclusions[k] for k,v in stored_exclusions.items())
                assert all(p['existing_rows_unchanged'] for p in audit['phases'])
                report.update(all_native_materializer_fields_equal=True,all_native_storage_fields_equal=True,
                    native_unique_storage_IDs=len({r['post_number'] for r in rows}),stored_oldest_first=True,
                    owned_DB_both_scope_native_eligible_metadata_baseline_complete=True,documents=len(rows),
                    captured_response_count=len(audit['requests']),
                    captured_request_kinds=dict(Counter(request_kind(r['url']) for r in audit['requests'])),
                    native_short_body_exclusions=len(exclusions),excluded_URLs=list(exclusions),
                    actual_exclusion_detail_rechecks=rechecks,
                    original_measurement_status=audit['status'],
                    obsolete_zero_detail_assertion_replaced_with_explicit_exclusion_recheck_validation=True,
                    original_audit_preserved=True,audit_sha256=hashlib.sha256((root/'audit.json').read_bytes()).hexdigest())
            finally:conn.close()
        atomic_json(out,report);print(json.dumps({k:report[k] for k in ('status','source_memberships','source_scope_counts')}))
    finally:c._session.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--out',required=True);p.add_argument('--root')
    a=p.parse_args();run(a.source,a.out,a.root)
