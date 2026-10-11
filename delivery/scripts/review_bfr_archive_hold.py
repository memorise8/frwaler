"""Replay the final BfR archive guard against captured source bytes, offline."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import zipfile

import requests

from crawler import db_libertree
from crawler.incremental import Collector, Policy, Store, UnsupportedSource, init_schema
from crawler.incremental_bfr import BfRAdapter
from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json, utc_now


def run(capture, out):
    capture, out = Path(capture), Path(out)
    out.mkdir(parents=True, exist_ok=False)
    audit = json.loads((capture/'audit.json').read_text())
    responses = {}
    for r in audit['requests']:
        raw = (capture/r['body']).read_bytes()
        assert r['status'] == 200 and hashlib.sha256(raw).hexdigest() == r['sha256']
        responses[r['url']] = raw
    archive_response = audit['requests'][-1]
    archive = responses[archive_response['url']]
    assert zipfile.is_zipfile(io.BytesIO(archive))
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        members = [{'name': f.filename, 'bytes': f.file_size, 'compressed_bytes': f.compress_size}
                   for f in z.infolist() if not f.is_dir()]
    os.environ['LIBERTREE_DB_BACKEND'] = 'sqlite'
    conn = db_libertree.open_db(out/'owned.sqlite')
    db_libertree.init_db(conn)
    init_schema(conn)
    cls = CRAWLERS['bfr-bund-de-en']
    db_libertree.upsert_site(conn, cls.site_id, cls.site_name, cls.base_url)
    c = cls(conn, delay=0)
    calls = []
    def request(url, **kwargs):
        url = requests.Request('GET', url).prepare().url
        calls.append(url)
        r = requests.Response()
        r._content, r._content_consumed, r.status_code, r.encoding = responses[url], True, 200, 'utf-8'
        return r
    c._request = request
    try:
        try:
            Collector(c, BfRAdapter(c), Policy(full_check_days=0)).run()
        except UnsupportedSource as exc:
            reason = str(exc)
        else:
            raise AssertionError('archive must not become an ordinary PDF or native exclusion')
        assert reason == 'bfr_download_archive_not_pdf_requires_primary_member_contract'
        assert calls == [r['url'] for r in audit['requests']]
        state = Store(conn, cls.site_id).state()
        assert state['support'] == 'full_rescan_required' and not state.get('baseline_complete', False)
        pending = conn.execute('SELECT count(*) FROM crawl_incremental_pending').fetchone()[0]
        assert pending > 0 and conn.execute('SELECT count(*) FROM documents').fetchone()[0] == 0
        report = {'status': 'passed_expected_archive_hold', 'reviewed_at': utc_now(),
                  'site_id': cls.site_id, 'classification': 'full_rescan_required', 'reason': reason,
                  'source_sha256': hashlib.sha256(Path(cls.crawl.__code__.co_filename).read_bytes()).hexdigest(),
                  'adapter_source_sha256': hashlib.sha256(Path(BfRAdapter.fetch.__code__.co_filename).read_bytes()).hexdigest(),
                  'captured_responses': len(calls), 'final_code_matches_captured_request_sequence': True,
                  'archive_source_url': archive_response['url'], 'archive_sha256': archive_response['sha256'],
                  'archive_bytes': len(archive), 'archive_members': members,
                  'PDF_members': sum(m['name'].lower().endswith('.pdf') for m in members),
                  'HTML_members': sum(m['name'].lower().endswith('.html') for m in members),
                  'pending_items_preserved': pending, 'baseline_complete': False,
                  'no_archive_members_extracted_to_filesystem': True, 'live_http_requests': 0,
                  'global_source_complete': False,
                  'note': 'Selecting an archive member as the report and associating its supporting files requires a separate source/file contract. The annual-only live test does not establish a completed baseline for both delivered scopes.'}
        atomic_json(out/'review.json', report)
        print(json.dumps({k: report[k] for k in ('status', 'reason', 'captured_responses', 'PDF_members', 'HTML_members', 'pending_items_preserved')}))
    finally:
        c._session.close()
        conn.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--capture', required=True)
    p.add_argument('--out', required=True)
    a = p.parse_args()
    run(a.capture, a.out)
