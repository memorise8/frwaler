"""Bounded, gated OAI replay against an already populated owned fixture DB."""
import argparse
import os
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote, urlsplit
import xml.etree.ElementTree as ET

from crawler import db_pg
from crawler.incremental import Store
from crawler.sites import CRAWLERS
from delivery.health.incremental_transport import install
from delivery.health.results import atomic_json, utc_now
from delivery.scripts.review_arrow_incremental import SITE, capture, retained


def run(dsn, base, out):
    if not urlsplit(dsn).path[1:].startswith('incremental_rollout_arrow_'):
        raise ValueError('owned Arrow database required')
    base, out = Path(base), Path(out)
    out.mkdir(parents=True, exist_ok=False)
    pages, papers, _ = retained(base / 'continuous-20261010')
    conn = db_pg.open_db(dsn)
    c = CRAWLERS[SITE](conn, delay=0)
    c.delivery_mode = 'full'
    before = [dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
    state = Store(conn, SITE).state()
    if (len(before) != 229 or {r['post_number'] for r in before} != {p['external_id'] for p in papers}
            or state['support'] != 'full_rescan_required' or state['baseline_complete']):
        raise ValueError('owned Arrow229 fallback fixture required')
    requests, attempts = [], []
    report = dict(status='running', started_at=utc_now(), requests=requests,
                  fixture_database=True, audited_session_replaces_native_curl=True,
                  operating_database_touched=False)
    def persist():
        atomic_json(out / 'audit.json', report)
    c._session.headers['Accept-Encoding'] = 'gzip, deflate'
    install(c, base / 'continuous-20261010/request-gate', out, requests, persist=persist)
    ns = c.crawl.__globals__['_NS']
    next_url = pages[0]['url']
    completed = 0
    def fetch(url, *args, **kwargs):
        nonlocal next_url, completed
        if url != next_url or completed >= 3:
            raise ValueError('Arrow OAI request budget or continuation mismatch')
        raw = c._session.get(url, timeout=40).content
        records, current = capture(raw)
        old_records, old = capture(pages[completed]['raw'])
        if len(records) != len(old_records) or current != old:
            raise ValueError('Arrow current metadata differs from owned fixture')
        token = ET.fromstring(raw).find('oai:ListRecords/oai:resumptionToken', ns)
        if token is None:
            raise ValueError('Arrow continuation contract missing')
        value = (token.text or '').strip()
        if bool(value) != (completed < 2):
            raise ValueError('Arrow unexpected source size')
        next_url = ('https://arrow.tudublin.ie/do/oai/?verb=ListRecords&resumptionToken='
                    + quote(value, safe='/')) if value else None
        completed += 1
        return raw
    native_save = c._save_paper
    def save(paper):
        attempts.append(paper['external_id'])
        return native_save(paper)
    c._save_paper = save
    persist()
    try:
        with patch.dict(os.environ, {'LIBERTREE_DB_BACKEND': 'postgres'}), \
                patch.dict(c.crawl.__globals__, {'_curl_get': fetch}):
            saved = c.crawl()
        after = [dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
        if saved or before != after or len(attempts) != 229 or completed != 3 or c.delivery_source_failure:
            raise ValueError('Arrow native repeat changed fixture rows or failed')
        report.update(status='passed', actual_HTTP_requests=len(requests), actual_detail_requests=0,
                      actual_PDF_requests=0, save_attempts=229, saved_new=0, rows=229,
                      source_OAI_records=231, native_abstract_exclusions=2,
                      existing_rows_unchanged=True, stop_condition='terminal_resumption_token',
                      known_item_early_stop=False, baseline_complete=False,
                      global_source_complete=False, all_files_verified=False, finished_at=utc_now())
    except Exception as exc:
        report.update(status='failed', error_type=type(exc).__name__, finished_at=utc_now())
        raise
    finally:
        persist()
        c._session.close()
        conn.close()
    print({k: report[k] for k in ('status', 'actual_HTTP_requests', 'save_attempts', 'saved_new')})


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--dsn', required=True)
    p.add_argument('--base', required=True)
    p.add_argument('--out', required=True)
    a = p.parse_args()
    run(a.dsn, a.base, a.out)
