"""Measure native fallback with retained BMLV list and synthetic detail responses."""
import json
import os
from pathlib import Path
import re
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

DSN = os.environ.get('INCREMENTAL_ROLLOUT_TEST_DSN')
HTML = os.environ.get('INCREMENTAL_BMLV_LIST_HTML')


@unittest.skipUnless(DSN and HTML, 'requires fresh owned PostgreSQL DB and BMLV type-14 list')
class BmlvWorkerTests(unittest.TestCase):
    def test_fallback_refetches_details_without_duplicate_saves_or_false_baseline(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs, worker
        from delivery.scripts.review_bmlv_incremental import parse_table
        self.assertTrue(urlsplit(DSN).path[1:].startswith('incremental_rollout_'))
        conn = db_pg.open_db(DSN)
        self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'])
        db_pg.init_db(conn)
        init_delivery_schema(conn)
        cls = CRAWLERS['bmlv-gv-at-wissen-forschung']
        db_pg.upsert_site(conn, cls.site_id, cls.site_name, cls.base_url)
        raw = Path(HTML).read_bytes()
        ids = {r['id'] for r in parse_table(raw)}
        self.assertEqual(len(ids), 18)
        helpers = cls.crawl.__globals__
        calls, phases = [], []

        def fetch(url, **kwargs):
            if url == helpers['_LIST_URL']:
                calls.append({'kind': 'list', 'url': url})
                return raw
            match = re.search(r'/(publikation|beitrag)\.php\?id=(\d+)$', url)
            self.assertIsNotNone(match)
            kind, pid = match.groups()
            self.assertIn(pid, ids)
            calls.append({'kind': kind, 'url': url})
            if kind == 'beitrag':
                return b'<html><div id="content"><div class="floatbox">Controlled contribution fixture</div></div></html>'
            return (f'<html><div id="content"><h2>Controlled publication {pid}</h2>'
                    '<p>Synthetic body fixture for repeat-request measurement and persistence checks only. '
                    'This does not verify the actual publication content.</p>'
                    '<div class="floatbox">Dokumenttyp: Berichte Erscheinungsdatum: 05.06.2008</div>'
                    f'<a href="beitrag.php?id={pid}">Contribution fixture</a></div></html>').encode()

        with patch.dict(os.environ, {'LIBERTREE_DB_BACKEND': 'postgres'}), \
                patch.dict(helpers, {'_curl_get': fetch}), patch.object(helpers['time'], 'sleep'), \
                patch.object(cls, 'DELIVERY_ORDER', 'newest_first'):
            baseline = None
            for label in ('initial', 'repeat'):
                start = len(calls)
                jobs.enqueue_job(conn, cls.site_id, mode='incremental')
                saved = worker.run_job(conn, jobs.claim_next_job(conn), delay=0)
                rows = [dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
                if baseline is not None:
                    self.assertEqual(rows, baseline)
                baseline = rows
                state = Store(conn, cls.site_id).state()
                self.assertFalse(state['baseline_complete'])
                self.assertEqual(state['support'], 'full_rescan_required')
                self.assertIn('missing_date', state['reason'])
                self.assertIn('order_review', state)
                phase = {'phase': label, 'saved_new': saved, 'rows': len(rows),
                         'transport_calls': len(calls)-start, 'baseline_complete': False}
                for kind in ('list', 'publikation', 'beitrag'):
                    phase[kind+'_calls'] = sum(r['kind'] == kind for r in calls[start:])
                phases.append(phase)
            self.assertEqual([p['saved_new'] for p in phases], [18, 0])
            self.assertEqual([p['transport_calls'] for p in phases], [37, 37])
            self.assertEqual([p['publikation_calls'] for p in phases], [18, 18])
            self.assertEqual([p['beitrag_calls'] for p in phases], [18, 18])
        output = os.environ.get('INCREMENTAL_BMLV_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists())
            Path(output).write_text(json.dumps({'status': 'passed', 'fixture_transport': True,
                'live_http_requests': 0, 'database': urlsplit(DSN).path[1:], 'phases': phases,
                'retained_source_list_items': 18, 'detail_and_contribution_responses_synthetic': True,
                'existing_rows_unchanged': True, 'global_source_complete': False,
                'unconfigured_delivered_publications': 217, 'source_documents_deleted': False}, indent=2)+'\n')


if __name__ == '__main__':
    unittest.main()
