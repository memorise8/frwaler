"""Independently reconcile captured ONERA responses with isolated stored rows."""
import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import sqlite3

import requests

from crawler.incremental import observe_order, infer_order
from crawler.incremental_onera import OneraAdapter
from crawler.document_dates import normalize_document_dates
from crawler.libertree_adapter import paper_to_document
from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json, utc_now
from delivery.scripts.measure_onera_incremental import request_kind


def retained(source, probe, out):
    source, probe = Path(source), Path(probe)
    prior = json.loads((source/'onera-listing-scope-independent-review.json').read_text())
    audit_path = source/'onera-listing-scope/audit.json'
    assert hashlib.sha256(audit_path.read_bytes()).hexdigest() == prior['audit_sha256']
    manifest = source/prior['manifest_file']
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == prior['manifest_sha256']
    expected = list(csv.DictReader(manifest.open()))
    responses = {}
    for p in json.loads(audit_path.read_text())['pages']:
        raw = (audit_path.parent/p['file']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == p['sha256']
        responses[p['url']] = raw
    # Prior observation had only the linked final page. The adjacent explicit
    # empty view is separately labelled as a fresh observation, not old evidence.
    probe_audit = json.loads((probe/'audit.json').read_text())
    end = next(r for r in probe_audit['requests'] if r['url'].endswith('?page=14'))
    raw = (probe/end['body']).read_bytes()
    assert end['status'] == 200 and hashlib.sha256(raw).hexdigest() == end['sha256']
    responses[end['url']] = raw
    c = CRAWLERS['onera-fr-fr'](None, delay=0)
    def response(url, **kwargs):
        r = requests.Response()
        r._content = responses[url]
        r.encoding = 'utf-8'
        return r
    c._request = response
    try:
        a = OneraAdapter(c)
        cursor, entries, proof = None, [], {}
        while True:
            page = a.fetch(cursor, 'native')
            entries.extend(page.entries)
            observe_order(proof, page.entries, a.numeric_ids)
            cursor = page.next_cursor
            if page.exhausted:
                break
        assert len(entries) == len({e.key for e in entries}) == len(expected) == 168
        for e, r in zip(entries, expected):
            assert e.key == r['url'] and e.payload['title'] == c.crawl.__globals__['_compact_text'](r['title'])
            assert e.payload['listed_date_raw'] == r['listed_date_raw']
        assert infer_order(proof) == ('newest_first', 'date')
        report = {'status': 'passed', 'reviewed_at': utc_now(), 'retained_memberships': 168,
                  'retained_listing_pages': 14, 'native_list_fields_match_manifest': True,
                  'manifest_title_comparison_normalizer': 'native _compact_text; two titles contain doubled spaces',
                  'order': infer_order(proof), 'proof': proof, 'source_hashes_verified': True,
                  'adjacent_empty_evidence': 'separate fresh probe page=14', 'live_http_requests': 0,
                  'global_source_complete': False}
        atomic_json(Path(out), report)
        print(json.dumps(report))
    finally:
        c._session.close()


def live(root, source, out):
    root, source = Path(root), Path(source)
    audit = json.loads((root/'audit.json').read_text())
    assert audit['status'] == 'passed_scoped_live_initial_resume_repeat'
    responses = {}
    for r in audit['requests']:
        raw = (root/r['body']).read_bytes()
        assert r['status'] == 200 and hashlib.sha256(raw).hexdigest() == r['sha256']
        responses[r['url']] = raw
    c = CRAWLERS['onera-fr-fr'](None, delay=0)
    c._fetch = lambda url, **kwargs: responses[url].decode('utf-8')
    pdf_reads = []
    def request(method, url, **kwargs):
        assert method.lower() == 'get' and request_kind(url) == 'pdf'
        pdf_reads.append(url)
        r = requests.Response()
        r._content, r._content_consumed, r.status_code = responses[url], True, 200
        return r
    c._session.request = request
    conn = sqlite3.connect('file:'+str(root/'owned.sqlite')+'?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cards, page = [], 0
        while True:
            url = c.base_url+'/fr/presse?page='+str(page)
            soup = c._soup(responses[url].decode('utf-8'), 'captured source')
            view = soup.select_one(OneraAdapter.view_selector)
            assert view is not None
            items = c._parse_list(view)
            if not items:
                assert view.select_one('.view-empty') is not None
                break
            assert (c._next_page(soup) == page+1) if page < 13 else c._next_page(soup) is None
            cards.extend(items)
            page += 1
        assert len(cards) == 168 and page == 14
        prior = json.loads((source/'onera-listing-scope-independent-review.json').read_text())
        manifest = source/prior['manifest_file']
        assert hashlib.sha256(manifest.read_bytes()).hexdigest() == prior['manifest_sha256']
        old_urls = {r['url'] for r in csv.DictReader(manifest.open())}
        by_url = {i['url']: i for i in cards}
        rows = list(conn.execute('SELECT * FROM documents ORDER BY seq_id'))
        assert len(rows) == len(by_url) == 168 and {r['meta_url'] for r in rows} == set(by_url) == old_urls
        dates = [by_url[r['meta_url']]['listed_date'] for r in rows]
        assert dates == sorted(dates)
        ids = []
        for row in rows:
            paper = c._materialize_item(by_url[row['meta_url']])
            assert paper is not None and not c.delivery_source_failure
            stored = paper_to_document(paper)
            stored = normalize_document_dates(dict(stored, listed_date=stored['posted_date']))
            assert row['post_number'] == paper['external_id']
            ids.append(paper['external_id'])
            for key in ('title', 'abstract', 'pdf_url', 'original_filename', 'publisher', 'keywords', 'authors',
                        'published_date', 'published_date_raw', 'listed_date', 'listed_date_raw'):
                assert (row[key] or '') == (stored[key] or ''), (row['meta_url'], key)
            assert row['meta_url'] == paper['url']
            assert row['pdf_downloaded'] == row['text_extracted'] == 0
        assert len(set(ids)) == 168 and all(i.isdecimal() for i in ids)
        assert conn.execute('SELECT count(*) FROM crawl_incremental_pending').fetchone()[0] == 0
        assert all(p['existing_rows_unchanged'] for p in audit['phases'])
        report = {'status': 'passed', 'reviewed_at': utc_now(), 'documents': 168, 'listing_pages': 14,
                  'all_native_fields_equal': True, 'all_native_node_ids_preserved': True,
                  'storage_normalizers': ['paper_to_document', 'posted_date to listed_date', 'normalize_document_dates'],
                  'delivered_oldest_first': True, 'all_members_match_retained_original_scope': True,
                  'all_response_hashes_verified': True, 'response_count': len(audit['requests']),
                  'request_kinds': dict(Counter(request_kind(r['url']) for r in audit['requests'])),
                  'PDF_enrichment_reextracted_from_captured_bytes': len(pdf_reads),
                  'isolated_entire_metadata_scope_baseline_complete': True,
                  'global_source_complete': False, 'all_files_reverified': False, 'live_http_requests': 0,
                  'source_documents_deleted': False, 'operating_database_touched': False}
        atomic_json(Path(out), report)
        print(json.dumps(report))
    finally:
        conn.close()
        c._session.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--source', required=True)
    p.add_argument('--out', required=True)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--probe')
    mode.add_argument('--root')
    a = p.parse_args()
    if a.root:
        live(a.root, a.source, a.out)
    else:
        retained(a.source, a.probe, a.out)
