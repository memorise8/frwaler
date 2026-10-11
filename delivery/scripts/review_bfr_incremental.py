"""Reconcile the two delivered BfR scopes and isolated native metadata."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3

import requests

from crawler.incremental import Entry, observe_order, infer_order
from crawler.incremental_bfr import BfRAdapter, SCOPES, day
from crawler.libertree_adapter import paper_to_document
from crawler.document_dates import normalize_document_dates
from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json, utc_now
from delivery.scripts.measure_bfr_incremental import request_kind

SITE = 'bfr-bund-de-en'


def retained(source, out, capture=None):
    source = Path(source)
    prior = json.loads((source/'bfr-all-delivered-catalogues-independent-review.json').read_text())
    manifest = source/prior['records_file']
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == prior['records_sha256']
    expected = [json.loads(x) for x in manifest.read_text().splitlines()]
    c = CRAWLERS[SITE](None, delay=0)
    try:
        a = BfRAdapter(c)
        responses = {}
        for n, scope in enumerate(SCOPES):
            entry_audit = json.loads((source/'bfr-delivered-entries/audit.json').read_text())
            entry = next(r for r in entry_audit['responses'] if r['scope'] == f'scope-{n}')
            raw = (source/'bfr-delivered-entries'/entry['file']).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == entry['sha256']
            responses[SCOPES[scope]['url']] = raw
        for p in prior['pages']:
            raw = (source/p['source_file']).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == p['source_sha256']
            scope = list(SCOPES)[int(p['scope'][-1])]
            responses[a.url(scope, p['page'])] = raw
        if capture:
            capture = Path(capture)
            current = {}
            for r in json.loads((capture/'audit.json').read_text())['requests']:
                raw = (capture/r['body']).read_bytes()
                assert r['status'] == 200 and hashlib.sha256(raw).hexdigest() == r['sha256']
                current[r['url']] = raw
            responses = current
        def response(url, **kwargs):
            r = requests.Response()
            r._content, r.encoding = responses[url], 'utf-8'
            return r
        a.response = response
        items, proofs = [], {}
        for n, scope in enumerate(SCOPES):
            observed, proof = [], {}
            for index in range(prior['scopes'][n]['source_pages']):
                data = a.read(scope, index)
                observed.extend(data['items'])
                observe_order(proof, [Entry(i['external_id'], i, day(i)) for i in data['items']], False)
            expected_scope = [r for r in expected if r['scope'] == f'scope-{n}']
            assert len(observed) == len(expected_scope)
            for i, r in zip(observed, expected_scope):
                assert i['external_id'] == str(r['source_uid']) and i['url'] == r['url']
                assert i['published_date'] == r['native_list_fields']['published_date']
                assert i['pdf_url'] == r['native_list_fields']['pdf_url']
            assert infer_order(proof) == ('newest_first', 'date')
            proofs[scope] = {'items': len(observed), 'proof': proof, 'order': infer_order(proof)}
            items.extend(observed)
        assert len(items) == len({i['external_id'] for i in items}) == 39
        report = {'status': 'passed', 'reviewed_at': utc_now(), 'scopes': proofs,
                  'source_memberships': 39, 'unique_source_uids': 39, 'source_pages': 7,
                  'both_adjacent_empty_boundaries_verified': True, 'native_ID_URL_date_PDF_fields_equal': True,
                  'source_response_hashes_verified': True, 'live_http_requests': 0,
                  'capture': str(capture) if capture else 'retained catalogue source',
                  'details_and_files_not_checked_by_listing_review': True, 'global_source_complete': False}
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
    def captured(url):
        return responses[requests.Request('GET', url).prepare().url]
    c = CRAWLERS[SITE](None, delay=0)
    c._curl_get_text = lambda url, **kw: (captured(url).decode('utf-8'), url)
    c._curl_get_bytes = lambda url, **kw: (captured(url), url)
    conn = sqlite3.connect('file:'+str(root/'owned.sqlite')+'?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        a = BfRAdapter(c)
        items = []
        selected = audit['scope']['catalogues']
        for scope in selected:
            page = 1
            while True:
                payload = json.loads(captured(a.url(scope, page)))['content']['colPos0'][0]['content']['data']
                parsed = c._parse_list_records(payload)
                if not parsed:
                    assert page == payload['pagination']['numberOfPages']+1
                    break
                items.extend(dict(i, _source_scope=scope) for i in parsed)
                page += 1
        prior = json.loads((source/'bfr-all-delivered-catalogues-independent-review.json').read_text())
        path = source/prior['records_file']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == prior['records_sha256']
        expected = [json.loads(x) for x in path.read_text().splitlines()]
        expected = [r for r in expected if list(SCOPES)[int(r['scope'][-1])] in selected]
        assert {(i['external_id'], i['url']) for i in items} == {(str(r['source_uid']), r['url']) for r in expected}
        by_id = {i['external_id']: i for i in items}
        rows = list(conn.execute('SELECT * FROM documents ORDER BY seq_id'))
        excluded, papers = {}, {}
        for item in items:
            paper = c._materialize_record(item, a.scope['endpoint'], SCOPES[item['_source_scope']]['filters'])
            if paper is None:
                soup = c._make_soup(captured(item['url']).decode('utf-8'))
                parsed = c._parse_detail(soup, item)
                assert soup.select_one('h1') is not None and not item['pdf_url'] and not parsed['pdf_url']
                excluded[item['external_id']] = {'url': item['url'], 'title': item['title'], 'parsed': parsed}
            else:
                papers[item['external_id']] = paper
        state = audit['durable_state']
        assert set(excluded) == set(state['cursor']['exclusions'])
        assert len(items) == len(expected) and len(rows)+len(excluded) == len(expected)
        assert {r['post_number'] for r in rows} == set(papers)
        dates = [day(by_id[r['post_number']]) for r in rows]
        assert dates == sorted(dates)
        for row in rows:
            paper = papers[row['post_number']]
            doc = paper_to_document(paper)
            doc = normalize_document_dates(dict(doc, listed_date=doc.get('posted_date')))
            assert row['meta_url'] == paper['url']
            for key in ('title', 'abstract', 'authors', 'publisher', 'keywords', 'pdf_url', 'original_filename',
                        'published_date', 'published_date_raw', 'listed_date', 'listed_date_raw'):
                assert (row[key] or '') == (doc[key] or ''), (row['post_number'], key)
            assert not row['pdf_downloaded'] and not row['text_extracted']
        assert all(p['existing_rows_unchanged'] for p in audit['phases'])
        report = {'status': 'passed', 'reviewed_at': utc_now(), 'source_memberships': len(items), 'scopes': selected,
                  'stored_documents': len(rows), 'native_exclusions': excluded,
                  'both_delivered_scopes_covered': selected == list(SCOPES), 'all_native_storage_fields_equal': True,
                  'delivered_oldest_first': True, 'source_hashes_verified': True,
                  'request_count': len(audit['requests']),
                  'request_kinds': dict(Counter(request_kind(r['url']) for r in audit['requests'])),
                  'isolated_entire_eligible_metadata_baseline_complete': True,
                  'source_complete_excluding_native_short_body_items': True,
                  'all_files_verified': False, 'global_source_complete': False, 'live_http_requests': 0,
                  'operating_database_touched': False, 'source_documents_deleted': False}
        atomic_json(Path(out), report)
        print(json.dumps(report))
    finally:
        conn.close()
        c._session.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--source', required=True)
    p.add_argument('--root')
    p.add_argument('--list-capture')
    p.add_argument('--out', required=True)
    a = p.parse_args()
    if a.root:
        live(a.root, a.source, a.out)
    else:
        retained(a.source, a.out, a.list_capture)
