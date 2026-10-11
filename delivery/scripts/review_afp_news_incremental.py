"""Hash-verified native parser/storage comparison for the actual AFP date window."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs,urlsplit

from crawler.document_dates import normalize_document_dates
from crawler.incremental import Entry,observe_order,infer_order
from crawler.libertree_adapter import paper_to_document
from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json,utc_now


def run(root,out):
    root,out=Path(root),Path(out)
    if out.exists():raise FileExistsError(out)
    audit=json.loads((root/'audit.json').read_text())
    if audit['status']!='passed_scoped_live_initial_resume_repeat':raise ValueError('live phases not complete')
    responses={}
    for r in audit['requests']:
        raw=(root/r['body']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=r['sha256'] or r['status']!=200 or r['method'].upper()!='GET':
            raise ValueError('unexpected live response or body hash')
        responses[r['url']]=raw.decode()
    c=CRAWLERS['afp-gov-au-news-centre'](None,delay=0)
    conn=sqlite3.connect((root/'owned.sqlite').resolve().as_uri()+'?mode=ro',uri=True);conn.row_factory=sqlite3.Row
    try:
        lists=sorted([(int(parse_qs(urlsplit(u).query).get('page',['0'])[0]),u,raw)
            for u,raw in responses.items() if urlsplit(u).path=='/news-centre'])
        if [p for p,_,_ in lists]!=[0,1,2,3]:raise ValueError('scoped source traversal missing a page')
        items=[];counts=[]
        for page,url,raw in lists:
            q=parse_qs(urlsplit(url).query)
            if q.get('date_from')!=['2026-10-01'] or q.get('date_to')!=['2026-10-12']:
                raise ValueError('measurement scope changed')
            parsed,nxt=c._parse_list_page(raw,url);counts.append(len(parsed));items+=parsed
            if bool(nxt)!=(page<2):raise ValueError('scoped native pager mismatch')
        if counts!=[8,8,3,0] or len({i['node_id'] for i in items})!=19:
            raise ValueError('scoped membership count changed')
        proof={};observe_order(proof,[Entry(i['node_id'],i,i['listed_datetime']) for i in items],False)
        if infer_order(proof)!=('newest_first','date'):raise ValueError('scoped chronology unproven')
        rows=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
        by_url={r['meta_url']:r for r in rows}
        if set(by_url)!={i['url'] for i in items}:raise ValueError('stored source membership mismatch')
        def no_head(url):raise AssertionError('unrecorded native HEAD request')
        c._curl_head_content_disposition=no_head
        checked=('title','abstract','published_date','published_date_raw','listed_date','listed_date_raw',
                 'pdf_url','original_filename','authors','publisher','keywords')
        for item in items:
            detail=c._parse_detail_page(responses[item['url']],item['url']);paper=c._build_paper(item,detail)
            row=by_url[item['url']]
            if row['post_number']!=paper['external_id'] or row['post_number']!=item['node_id']:
                raise ValueError('native/list/stored identity mismatch')
            doc=paper_to_document(paper);doc=normalize_document_dates(dict(doc,listed_date=doc.get('posted_date')))
            for key in checked:
                if (row[key] or '')!=(doc[key] or ''):raise ValueError('native storage field mismatch: '+key)
        source_dates={i['node_id']:i['listed_datetime'] for i in items}
        order=[source_dates[r['post_number']] for r in rows]
        if order!=sorted(order):raise ValueError('delivery was not oldest first')
        phases=audit['phases']
        if [p['http_requests'] for p in phases]!=[9,16,3] or [p['saved_new'] for p in phases]!=[3,16,0]:
            raise ValueError('scoped actual request counts changed')
        report=dict(status='passed',reviewed_at=utc_now(),scope=audit['scope'],actual_scoped_metadata=19,
            native_parsed_page_counts=counts,order_proof=proof,all19_native_fields_equal=True,
            checked_storage_fields=list(checked)+['post_number','meta_url'],oldest_first=True,
            actual_live_HTTP_requests=28,actual_phase_requests=[9,16,3],actual_phase_saved_new=[3,16,0],
            actual_phase_detail_requests=[3,16,0],synthetic_source_responses=False,
            source_response_hashes_verified=len(audit['requests']),
            audit_sha256=hashlib.sha256((root/'audit.json').read_bytes()).hexdigest(),
            global_source_complete=False,entire_news_history_verified=False,all_files_verified=False,
            source_documents_deleted=False,operating_database_touched=False)
        atomic_json(out,report);print(json.dumps({'status':'passed','actual_scoped_metadata':19,'actual_live_HTTP_requests':28}))
    finally:c._session.close();conn.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();run(a.root,a.out)
