"""Verify retained original ADR ranges and merged card order without live HTTP."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace

from crawler import db_libertree
from crawler.incremental import observe_order, infer_order, init_schema
from crawler.incremental_adruk import AdrukAdapter
from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json, utc_now


def run(source, out):
    source = Path(source); root = source / 'adruk-delivery-scope-enumeration-v2'
    audit_path = root / 'audit.json'; audit = json.loads(audit_path.read_text())
    prior = json.loads((source / 'adruk-delivery-enumeration-v2-independent-review.json').read_text())
    manifest = source / prior['manifest']
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == prior['manifest_sha256']
    assert hashlib.sha256(audit_path.read_bytes()).hexdigest() == prior['audit_sha256']
    expected = [json.loads(x) for x in manifest.read_text().splitlines()]
    responses = {}
    for page in audit['pages']:
        raw = (root / page['file']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == page['sha256']
        responses[page['url']] = raw.decode('utf-8', errors='replace')
    os.environ['LIBERTREE_DB_BACKEND'] = 'sqlite'
    with tempfile.TemporaryDirectory() as temp:
        conn = db_libertree.open_db(Path(temp) / 'owned.sqlite'); db_libertree.init_db(conn); init_schema(conn)
        c = CRAWLERS['adruk-org-news-publications'](conn, delay=0)
        try:
            c._request = lambda url, **kw: SimpleNamespace(text=responses[url])
            # This replay certifies list membership only. Real annual alias/body
            # reconciliation is measured separately in the isolated live run.
            c._annual_sources = lambda: {}
            a = AdrukAdapter(c); cursor = None; entries = []; proof = {}
            while True:
                page = a.fetch(cursor, 'native'); entries.extend(page.entries)
                observe_order(proof, page.entries, False); cursor = page.next_cursor
                if page.exhausted: break
            assert len(entries) == len({e.key for e in entries}) == 341
            by_url = {r['url']:r for r in expected}
            assert {e.payload['url'] for e in entries} == set(by_url)
            fields = {'slug':'slug', 'list_title':'title', 'listed_date_raw':'published_date_raw',
                      'list_abstract':'listed_abstract', '_source_scope':'scope'}
            for e in entries:
                row = by_url[e.payload['url']]
                assert all(e.payload[k] == row[v] for k, v in fields.items())
                assert e.payload['direct_pdf'] == (row['link_kind'] == 'direct_pdf')
            assert infer_order(proof) == ('newest_first', 'date')
            report = dict(status='passed', reviewed_at=utc_now(), retained_pages=len(audit['pages']),
                          source_records=len(entries), scope_counts={s:sum(e.payload['_source_scope']==s for e in entries) for s in a.scope['scopes']},
                          manifest_sha256=prior['manifest_sha256'], all_response_hashes_verified=True,
                          all_native_list_payloads_equal=True, merged_order=infer_order(proof), proof=proof,
                          annual_alias_reconciliation_checked_by_this_replay=False, live_http_requests=0,
                          global_source_complete=False)
            atomic_json(Path(out), report); print(json.dumps(report)); return report
        finally:
            c._session.close(); conn.close()


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--out',required=True)
    args=p.parse_args();run(args.source,args.out)
