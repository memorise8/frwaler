"""Read-only independent replay of captured ADR responses and stored fields."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3

from crawler.document_dates import normalize_date
from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json, utc_now
from delivery.scripts.measure_adruk_incremental import request_kind


def run(root, source, out):
    root, source = Path(root), Path(source)
    audit = json.loads((root / 'audit.json').read_text())
    assert audit['status'] == 'passed_scoped_live_initial_resume_repeat'
    responses = {}
    for r in audit['requests']:
        raw = (root / r['body']).read_bytes()
        assert r['status'] == 200 and hashlib.sha256(raw).hexdigest() == r['sha256']
        responses[r['url']] = raw
    c = CRAWLERS['adruk-org-news-publications'](None, delay=0); helpers = c.crawl.__globals__
    c._html = lambda url: responses[url].decode('utf-8', errors='replace')
    c._pdf_bytes = lambda url: responses[url]
    conn = sqlite3.connect('file:' + str(root / 'owned.sqlite') + '?mode=ro', uri=True); conn.row_factory = sqlite3.Row
    try:
        annual = c._annual_sources(); assert len(annual) == 5
        listing = []; page = 0
        while True:
            data = helpers['_listing'](c._html(helpers['_list_url']('publications', page)), 'publications', page)
            assert data['first'] == len(listing) + 1
            listing.extend(data['items'])
            if data['last'] == data['total']: break
            page += 1
        manifest = source / 'adruk-delivery-source-records.jsonl'
        expected = [json.loads(line) for line in manifest.read_text().splitlines()]
        expected = {r['slug'] for r in expected if r['scope'] == 'publications'}
        rows = list(conn.execute('SELECT * FROM documents ORDER BY seq_id'))
        assert len(rows) == len(listing) == len(expected) == 224
        assert {r['post_number'] for r in rows} == {r['slug'] for r in listing} == expected
        by_id = {r['slug']:r for r in listing}; represented = set(); pdf_body_ids = []
        dates = [normalize_date(by_id[r['post_number']]['listed_date_raw'])['value'] for r in rows]
        assert dates == sorted(dates)
        for row in rows:
            item = by_id[row['post_number']]
            paper, matched, provenance = c._materialize(item, 'publications', annual)
            for k in ('title','abstract','pdf_url','original_filename','publisher','keywords'):
                assert (row[k] or '') == (paper[k] or ''), (row['post_number'], k)
            assert row['meta_url'] == paper['url']
            assert row['published_date_raw'] == paper['published_date']
            assert row['listed_date_raw'] == paper['posted_date']
            assert row['pdf_downloaded'] == row['text_extracted'] == 0
            if matched: represented.add(matched)
            if provenance: pdf_body_ids.append(row['post_number'])
        assert represented == set(annual)
        assert conn.execute('SELECT count(*) FROM crawl_incremental_pending').fetchone()[0] == 0
        report = dict(status='passed', reviewed_at=utc_now(), documents=224, listing_pages=page+1,
                      all_response_hashes_verified=True, response_count=len(audit['requests']),
                      request_kinds=dict(Counter(request_kind(r['url']) for r in audit['requests'])),
                      all_native_fields_equal=True, delivered_oldest_first=True, annual_aliases=5,
                      annual_roots_are_not_extra_documents=True, PDF_excerpt_recovered_ids=pdf_body_ids,
                      matches_retained_original_publication_scope=True, existing_rows_unchanged_each_phase=True,
                      isolated_scope_baseline_complete=True, global_source_complete=False,
                      full_files_verified=False, source_documents_deleted=False, operating_database_touched=False)
        assert all(p['existing_rows_unchanged'] for p in audit['phases'])
        atomic_json(Path(out), report); print(json.dumps(report)); return report
    finally:
        conn.close(); c._session.close()


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--source',required=True);p.add_argument('--out',required=True)
    args=p.parse_args();run(args.root,args.source,args.out)
