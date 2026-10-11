"""Native fallback measurement using hash-verified actual CANURB captures."""
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

DSN = os.environ.get('INCREMENTAL_ROLLOUT_TEST_DSN')
SOURCE = os.environ.get('INCREMENTAL_CANURB_SOURCE')


@unittest.skipUnless(DSN and SOURCE, 'requires fresh owned PostgreSQL DB and retained CANURB source')
class CanurbWorkerTests(unittest.TestCase):
    def test_native_repeat_reads_all_details_without_duplicate_saves_or_false_baseline(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs, worker
        self.assertTrue(urlsplit(DSN).path[1:].startswith('incremental_rollout_'))
        conn = db_pg.open_db(DSN)
        self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'])
        db_pg.init_db(conn)
        init_delivery_schema(conn)
        cls = CRAWLERS['canurb-org-publications']
        db_pg.upsert_site(conn, cls.site_id, cls.site_name, cls.base_url)
        source = Path(SOURCE)
        prior = json.loads((source/'canurb-observed-persistence-independent-review.json').read_text())
        root = source/'canurb-observed-persistence'
        audit_path = root/'audit.json'
        self.assertEqual(hashlib.sha256(audit_path.read_bytes()).hexdigest(), prior['audit_sha256'])
        audit = json.loads(audit_path.read_text())
        responses, redirects = {}, []
        for url, entry in audit['responses'].items():
            raw = (root/entry['file']).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), entry['sha256'])
            if entry['final_url'] != url:
                redirects.append({'requested_url': url, 'captured_final_url': entry['final_url']})
            responses[url] = raw.decode(entry['encoding'])
        self.assertEqual(len(responses), 97)
        calls, phases = [], []
        def fetch(c, url, **kwargs):
            calls.append(url)
            return responses[url]
        with patch.dict(os.environ, {'LIBERTREE_DB_BACKEND': 'postgres'}), \
                patch.object(cls, '_curl_get', fetch), patch.object(cls.crawl.__globals__['time'], 'sleep'), \
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
                self.assertIn('nonmonotonic', state['reason'])
                self.assertIn('order_review', state)
                phases.append({'phase': label, 'saved_new': saved, 'rows': len(rows),
                    'transport_calls': len(calls)-start, 'detail_calls': sum(u != cls.base_url+'/publications/' for u in calls[start:]),
                    'baseline_complete': False})
            self.assertEqual([p['saved_new'] for p in phases], [98, 0])
            self.assertEqual([p['transport_calls'] for p in phases], [97, 97])
            self.assertEqual([p['detail_calls'] for p in phases], [96, 96])
            expected = {(r['post_id'], r['url']) for r in audit['source_records']}
            self.assertEqual({(r['post_number'], r['meta_url']) for r in rows}, expected)
            self.assertEqual(sum(r['published_date_precision'] == 'month' for r in rows), 85)
            self.assertEqual(sum(r['published_date_precision'] == 'year' for r in rows), 5)
        output = os.environ.get('INCREMENTAL_CANURB_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists())
            Path(output).write_text(json.dumps({'status': 'passed', 'fixture_transport': True,
                'actual_retained_HTTP_bodies': True, 'synthetic_source_responses': False,
                'live_http_requests': 0, 'database': urlsplit(DSN).path[1:], 'phases': phases,
                'retained_response_hashes_verified': 97, 'retained_source_list_items': 98,
                'retained_redirects': redirects, 'redirect_destinations_not_recrawled': True,
                'all_source_memberships_persisted': True, 'month_precision_preserved': 85,
                'year_precision_preserved': 5, 'existing_rows_unchanged': True,
                'global_source_complete': False, 'all_files_verified': False,
                'source_documents_deleted': False}, indent=2)+'\n')


if __name__ == '__main__':
    unittest.main()
