"""Read-only field/identity comparison against actual captured native details."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3

from crawler.sites import CRAWLERS
from crawler.libertree_adapter import paper_to_document
from crawler.document_dates import normalize_date
from delivery.health.results import atomic_json,utc_now
from delivery.scripts.measure_audencia_incremental import request_kind


def run(root,source,out):
    root,source=Path(root),Path(source);audit=json.loads((root/'audit.json').read_text())
    assert audit['status']=='passed_scoped_live_initial_resume_repeat';responses={}
    for r in audit['requests']:
        raw=(root/r['body']).read_bytes();assert r['status']==200 and hashlib.sha256(raw).hexdigest()==r['sha256']
        responses[r['url']]=raw.decode('utf-8')
    c=CRAWLERS['audencia-com-lecole'](None,delay=0)
    conn=sqlite3.connect('file:'+str(root/'owned.sqlite')+'?mode=ro',uri=True);conn.row_factory=sqlite3.Row
    try:
        cards=[];page=0
        while True:
            url=c.START_URL+f'?categories=56&page={page}';soup=c._make_soup(responses[url]);view=soup.select_one('.view-display-id-press_releases')
            assert view.select_one('input[name="categories"][checked]')['value']=='56'
            assert view.select_one('.total-view-results').get_text(strip=True)=='82'
            parsed=c._parse_list(soup,url)
            if not parsed:
                assert view.select_one('.view-empty').get_text(' ',strip=True)=='Votre recherche ne retourne aucun résultat.'
                break
            cards.extend(parsed);page+=1
        assert len(cards)==82 and page==7
        prior=json.loads((source/'audencia-all21-catalogue-independent-review.json').read_text());manifest=source/prior['records_file']
        assert hashlib.sha256(manifest.read_bytes()).hexdigest()==prior['records_sha256']
        original={json.loads(x)['url'] for x in manifest.read_text().splitlines()};by_url={i['url']:i for i in cards}
        rows=list(conn.execute('SELECT * FROM documents ORDER BY seq_id'))
        assert len(rows)==len(by_url)==82 and {r['meta_url'] for r in rows}==set(by_url)<=original
        dates=[normalize_date(by_url[r['meta_url']]['listed_datetime'])['value'] for r in rows];assert dates==sorted(dates)
        node_ids=[]
        for row in rows:
            card=by_url[row['meta_url']];raw=responses[card['url']]
            native=c._parse_detail(c._make_soup(raw),raw,card);paper=c._build_paper(native)
            stored=paper_to_document(paper)
            assert row['post_number']==paper['external_id'];node_ids.append(native['metadata']['node_id'])
            for k in ('title','abstract','pdf_url','original_filename','publisher','keywords','authors'):
                assert (row[k] or '')==(stored[k] or ''),(row['meta_url'],k)
            assert row['meta_url']==paper['url'] and row['published_date_raw']==paper['published_date'] and row['listed_date_raw']==paper['posted_date']
            assert row['pdf_downloaded']==row['text_extracted']==0
        assert len(set(node_ids))==82 and all(x.isdecimal() for x in node_ids)
        assert conn.execute('SELECT count(*) FROM crawl_incremental_pending').fetchone()[0]==0
        assert all(p['existing_rows_unchanged'] for p in audit['phases'])
        report=dict(status='passed',reviewed_at=utc_now(),documents=82,listing_pages=7,adjacent_empty_page_verified=True,
                    response_count=len(audit['requests']),all_response_hashes_verified=True,
                    request_kinds=dict(Counter(request_kind(r['url']) for r in audit['requests'])),
                    all_native_storage_fields_equal=True,existing_storage_normalizer="paper_to_document",all82_native_Drupal_node_IDs_preserved=True,
                    discovery_identity='original_detail_URL',delivered_oldest_first=True,
                    all_members_are_in_retained_original246=True,existing_rows_unchanged_each_phase=True,
                    isolated_scope_baseline_complete=True,global_source_complete=False,
                    full_files_verified=False,source_documents_deleted=False,operating_database_touched=False)
        atomic_json(Path(out),report);print(json.dumps(report));return report
    finally:conn.close();c._session.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--source',required=True);p.add_argument('--out',required=True)
    args=p.parse_args();run(args.root,args.source,args.out)
