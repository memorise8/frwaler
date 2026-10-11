"""Independent native-parser and storage comparison against captured press HTML."""
import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sqlite3
from unittest.mock import Mock, patch

from crawler.document_dates import normalize_document_dates
from crawler.incremental import Entry, infer_order, observe_order
from crawler.incremental_archives_nationales import ArchivesNationalesAdapter
from crawler.libertree_adapter import paper_to_document
from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json, utc_now

SITE = 'archives-nationales-culture-gouv-fr-presse'


def retained_responses(source):
    source = Path(source)
    prior = json.loads((source/'archives-nationales-delivered-scope-independent-review.json').read_text())
    manifest = source/prior['records_file']
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == prior['records_sha256']
    c = CRAWLERS[SITE](None, delay=0)
    try:
        a = ArchivesNationalesAdapter(c)
        responses = {}
        for page in prior['pages']:
            raw = (source/page['source_file']).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == page['source_sha256']
            responses[a.url(page['page'])] = raw.decode('utf-8')
        return responses, [json.loads(line) for line in manifest.read_text().splitlines()]
    finally:
        c._session.close()


def native_papers(responses):
    c = CRAWLERS[SITE](None, delay=0)
    papers = []
    c._fetch_page = lambda url: responses[url]
    def save(paper):
        papers.append(paper); c._last_save_created = True
    c._save_paper = save
    try:
        with patch.object(c.crawl.__globals__['time'], 'sleep'), contextlib.redirect_stdout(io.StringIO()):
            assert c.crawl() == len(papers)
        return papers
    finally:
        c._session.close()


def run(source, out, root=None):
    out = Path(out)
    if out.exists(): raise FileExistsError(out)
    responses, manifest = retained_responses(source)
    capture_count = 0
    if root:
        root = Path(root)
        audit = json.loads((root/'audit.json').read_text())
        assert audit['status'] == 'passed_scoped_live_initial_resume_repeat'
        responses = {}
        for request in audit['requests']:
            raw = (root/request['body']).read_bytes()
            assert request['status'] == 200 and hashlib.sha256(raw).hexdigest() == request['sha256']
            html = raw.decode('utf-8')
            # Dynamic HTML may differ; later parser comparisons validate the
            # actual fields. Never silently replace a different captured URL.
            responses[request['url']] = html
        capture_count = len(audit['requests'])
    papers = native_papers(responses)
    c = CRAWLERS[SITE](None, delay=0)
    try:
        c._request = lambda url, **kw: Mock(text=responses[url])
        a = ArchivesNationalesAdapter(c)
        items = []
        for page in range(4):
            data, has_next, _ = a.read(page)
            assert len(data) == (8 if page < 3 else 3) and has_next == (page < 3)
            items.extend(data)
        assert a.read(4, empty_after=3)[0] == []
        assert len(items) == len(manifest) == 27
        assert [i['paper'] for i in items if i['eligible']] == papers
        assert len(papers) == 16 and len({i['key'] for i in items}) == 27
        for item, old in zip(items, manifest):
            assert item['key'] == old['source_VersionId']
            assert item['paper']['url'] == old['source_file_url']
            assert item['paper']['title'] == old['title_raw']
            assert item['paper']['abstract'] == old['card_description_raw']
            assert json.loads(item['paper']['metadata'])['raw_date'] == old['publication_date_text_raw']
        proof = {}; observe_order(proof, [Entry(i['key'], i, i['date']) for i in items], True)
        assert infer_order(proof) == ('newest_first', 'date')
        assert proof['id']['up'] > 0
        report = dict(status='passed', reviewed_at=utc_now(), site_id=SITE,
            source_sha256=hashlib.sha256(Path(c.crawl.__code__.co_filename).read_bytes()).hexdigest(),
            adapter_source_sha256=hashlib.sha256(Path(a.fetch.__code__.co_filename).read_bytes()).hexdigest(),
            source_memberships=27, eligible_native_metadata=16, native_short_description_exclusions=11,
            exact_delivered_filter=True, native_parser_all_fields_equal=True,
            all_members_match_retained_original_scope=True, source_exhaustion='four pages plus adjacent empty page',
            order_proof=proof, version_ID_used_for_identity_not_order=True,
            source_PDF_bodies_verified=False, global_source_complete=False, live_http_requests=0,
            capture_responses_verified=capture_count, operating_database_touched=False, source_documents_deleted=False)
        if root:
            conn = sqlite3.connect('file:'+str(root/'owned.sqlite')+'?mode=ro', uri=True)
            conn.row_factory = sqlite3.Row
            try:
                rows = list(conn.execute('SELECT * FROM documents ORDER BY seq_id'))
                expected = {p['external_id']: p for p in papers}
                assert len(rows) == len(expected) == 16
                assert [r['published_date'] for r in rows] == sorted(r['published_date'] for r in rows)
                for row in rows:
                    p = expected[row['post_number']]
                    doc = paper_to_document(p)
                    doc = normalize_document_dates(dict(doc, listed_date=doc.get('posted_date')))
                    assert row['meta_url'] == p['url']
                    for key in ('title', 'abstract', 'pdf_url', 'original_filename', 'authors', 'publisher', 'keywords',
                                'published_date', 'published_date_raw', 'listed_date', 'listed_date_raw'):
                        assert (row[key] or '') == (doc[key] or ''), (p['external_id'], key)
                    assert not row['pdf_downloaded'] and not row['text_extracted']
                state = audit['durable_state']
                assert state['baseline_complete'] and state['order'] == 'newest_first'
                assert len(state['cursor']['exclusions']) == 11
                assert conn.execute('SELECT count(*) FROM crawl_incremental_pending').fetchone()[0] == 0
                assert all(p['existing_rows_unchanged'] for p in audit['phases'])
                report.update(owned_DB_native_eligible_baseline_complete=True, all_native_stored_fields_equal=True,
                              stored_oldest_first=True, stored_metadata=16, existing_rows_unchanged=True)
            finally: conn.close()
        atomic_json(out, report)
        print(json.dumps({k: report[k] for k in ('status', 'source_memberships', 'eligible_native_metadata', 'native_short_description_exclusions')}))
    finally: c._session.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--source', required=True); p.add_argument('--out', required=True)
    p.add_argument('--root'); a = p.parse_args(); run(a.source, a.out, a.root)
