"""Read-only reconciliation of UN Geneva listing order and captured native fields."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3

from crawler.incremental import Entry, observe_order, infer_order
from crawler.incremental_ungeneva import UNGenevaAdapter
from crawler.libertree_adapter import paper_to_document
from crawler.document_dates import normalize_document_dates
from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json, utc_now
from delivery.scripts.measure_ungeneva_incremental import request_kind

SITE = 'ungeneva-org-en'


def records(source):
    prior = json.loads((source/'ungeneva-all92-source-scope-independent-review.json').read_text())
    path = source/prior['source_records_file']
    assert hashlib.sha256(path.read_bytes()).hexdigest() == prior['source_records_sha256']
    return prior, [json.loads(line) for line in path.read_text().splitlines()]


def retained(source, out):
    source = Path(source)
    prior, expected = records(source)
    audit_path = source/'ungeneva-press-catalogue-v2/audit.json'
    boundary_path = source/'ungeneva-press-terminal-boundary/audit.json'
    assert hashlib.sha256(audit_path.read_bytes()).hexdigest() == prior['catalogue_audit_sha256']
    assert hashlib.sha256(boundary_path.read_bytes()).hexdigest() == prior['terminal_boundary_audit_sha256']
    responses = {}
    for p in json.loads(audit_path.read_text())['pages']:
        raw = (source/p['file']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == p['sha256']
        responses[p['url']] = raw.decode('utf-8')
    boundary = json.loads(boundary_path.read_text())
    raw = (boundary_path.parent/boundary['file']).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == boundary['sha256']
    responses[boundary['url']] = raw.decode('utf-8')
    c = CRAWLERS[SITE](None, delay=0)
    try:
        adapter = UNGenevaAdapter(c)
        adapter.html = lambda url, **kwargs: responses[url]
        items, proof = [], {}
        for n in range(10):
            data = adapter.read(n)
            assert data['has_next'] == (n < 9)
            items.extend(data['items'])
            observe_order(proof, [Entry(i['key'], i, i['listed_date']) for i in data['items']], False)
        assert not adapter.read(10, empty_after=9)['items']
        assert len(items) == len({i['key'] for i in items}) == len(expected) == 92
        for i, r in zip(items, expected):
            assert (i['url'], i['slug'], i['title'], i['listed_date']) == (r['url'], r['source_slug'], ' '.join(r['title'].split()), r['date_raw'])
        assert infer_order(proof) == ('newest_first', 'date')
        slugs = Counter(i['slug'] for i in items)
        report = {'status': 'passed', 'reviewed_at': utc_now(), 'retained_memberships': 92,
                  'unique_source_urls': 92, 'native_unique_slugs': len(slugs),
                  'reused_slug_groups': {k: v for k, v in slugs.items() if v > 1},
                  'listing_pages': 10, 'adjacent_empty_page_verified': True,
                  'native_source_fields_match_manifest': True, 'response_hashes_verified': True,
                  'title_comparison_normalizer': 'native whitespace folding; two manifest titles contain NBSP',
                  'order': infer_order(proof), 'proof': proof, 'live_http_requests': 0,
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
        responses[r['url']] = raw.decode('utf-8')
    c = CRAWLERS[SITE](None, delay=0)
    c._ung_fetch = lambda url: responses[url]
    conn = sqlite3.connect('file:'+str(root/'owned.sqlite')+'?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cards, n = [], 0
        while True:
            data = c._ung_page(n)
            if not data['items']:
                assert not data['has_next']
                break
            assert data['has_next'] == (n < 9)
            cards.extend(data['items'])
            n += 1
        assert len(cards) == 92 and n == 10
        _, expected = records(source)
        assert {i['url'] for i in cards} == {r['url'] for r in expected}
        by_url = {i['url']: i for i in cards}
        rows = list(conn.execute('SELECT * FROM documents ORDER BY seq_id'))
        assert len(rows) == len(by_url) == 92 and {r['meta_url'] for r in rows} == set(by_url)
        dates = [by_url[r['meta_url']]['listed_date'] for r in rows]
        assert dates == sorted(dates)
        canonical_aliases = []
        for row in rows:
            item = by_url[row['meta_url']]
            paper, exclusion = c._ung_detail(item)
            assert paper is not None and exclusion is None
            doc = paper_to_document(paper)
            doc = normalize_document_dates(dict(doc, listed_date=doc.get('posted_date')))
            assert row['post_number'] == item['slug'] == paper['external_id']
            assert row['meta_url'] == paper['url']
            for key in ('title', 'abstract', 'pdf_url', 'original_filename', 'authors', 'publisher', 'keywords',
                        'published_date', 'published_date_raw', 'listed_date', 'listed_date_raw'):
                assert (row[key] or '') == (doc[key] or ''), (row['meta_url'], key)
            canonical = c._ung_soup(responses[item['url']]).select_one('link[rel="canonical"]')['href']
            if canonical != item['url']:
                canonical_aliases.append({'source_url': item['url'], 'canonical': canonical})
            assert not row['pdf_downloaded'] and not row['text_extracted']
        assert conn.execute('SELECT count(*) FROM crawl_incremental_pending').fetchone()[0] == 0
        assert all(p['existing_rows_unchanged'] for p in audit['phases'])
        slugs = Counter(r['post_number'] for r in rows)
        report = {'status': 'passed', 'reviewed_at': utc_now(), 'documents': 92,
                  'unique_source_urls': 92, 'native_unique_slugs': len(slugs),
                  'reused_slug_groups': {k: v for k, v in slugs.items() if v > 1},
                  'canonical_aliases_preserved': canonical_aliases,
                  'all_native_fields_equal': True, 'native_pdf_url_remains_empty': True,
                  'storage_normalizers': ['paper_to_document', 'posted_date to listed_date', 'normalize_document_dates'],
                  'delivered_oldest_first': True, 'all_members_match_retained_original_scope': True,
                  'response_hashes_verified': True, 'response_count': len(audit['requests']),
                  'request_kinds': dict(Counter(request_kind(r['url']) for r in audit['requests'])),
                  'isolated_entire_metadata_scope_baseline_complete': True,
                  'all_files_verified': False, 'global_source_complete': False,
                  'source_documents_deleted': False, 'operating_database_touched': False, 'live_http_requests': 0}
        atomic_json(Path(out), report)
        print(json.dumps(report))
    finally:
        conn.close()
        c._session.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--source', required=True)
    p.add_argument('--root')
    p.add_argument('--out', required=True)
    a = p.parse_args()
    if a.root:
        live(a.root, a.source, a.out)
    else:
        retained(a.source, a.out)
