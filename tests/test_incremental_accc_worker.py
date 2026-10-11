"""Replay captured access refusals through native curl and the isolated worker."""
import contextlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

DSN = os.environ.get('INCREMENTAL_ROLLOUT_TEST_DSN')
SOURCE = os.environ.get('INCREMENTAL_ACCC_SOURCE')


@unittest.skipUnless(DSN and SOURCE, 'requires fresh owned PostgreSQL DB and captured ACCC responses')
class AcccWorkerTests(unittest.TestCase):
    def test_initial_and_repeat_access_failure_never_certifies_completion(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.scripts.review_accc_incremental import captures, SITES
        from delivery.worker import jobs, worker
        self.assertTrue(urlsplit(DSN).path[1:].startswith('incremental_rollout_'))
        conn = db_pg.open_db(DSN); self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'])
        db_pg.init_db(conn); init_delivery_schema(conn)
        responses = captures(SOURCE); results = []
        for site, url in SITES.items():
            cls = CRAWLERS[site]; db_pg.upsert_site(conn, site, cls.site_name, cls.base_url)
            calls = []; phases = []

            def fetched(cmd, **kwargs):
                self.assertEqual(cmd[0], 'curl'); self.assertEqual(cmd[-1], url)
                calls.append(url)
                return SimpleNamespace(returncode=0, stdout=responses[url]['body'], stderr=b'')

            with patch.dict(os.environ, {'LIBERTREE_DB_BACKEND': 'postgres'}), \
                    patch.object(cls.crawl.__globals__['subprocess'], 'run', fetched), \
                    patch('requests.Session.request', side_effect=AssertionError('live network forbidden')), \
                    contextlib.redirect_stdout(io.StringIO()):
                for label in ('initial', 'repeat'):
                    start = len(calls); jobs.enqueue_job(conn, site, mode='incremental')
                    saved = worker.run_job(conn, jobs.claim_next_job(conn), delay=0)
                    job = conn.execute('SELECT * FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()
                    self.assertEqual(job['status'], 'failed')
                    self.assertIn('authentication_failed', str(dict(job)))
                    self.assertEqual(saved, 0)
                    self.assertEqual(conn.execute('SELECT count(*) AS n FROM documents').fetchone()['n'], 0)
                    state = Store(conn, site).state()
                    self.assertEqual(state['support'], 'full_rescan_required')
                    self.assertFalse(state['baseline_complete'])
                    self.assertEqual(len(calls)-start, 1)
                    self.assertEqual(conn.execute("SELECT count(*) AS n FROM crawl_jobs WHERE status='queued'").fetchone()['n'], 0)
                    phases.append(dict(phase=label, saved_new=0, rows=0, native_HTTP_command_invocations=1,
                                       detail_calls=0, job_status='failed', baseline_complete=False))
            results.append(dict(site_id=site, phases=phases, response_sha256=responses[url]['sha256']))
        output = os.environ.get('INCREMENTAL_ACCC_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists())
            Path(output).write_text(json.dumps(dict(status='passed_expected_access_failure', sites=results,
                fixture_transport=True, actual_captured_denial_bytes=True, live_http_requests=0,
                database=urlsplit(DSN).path[1:], request_count_boundary='intercepted native curl subprocess invocations',
                source_exhaustion_observed=False, global_source_complete=False,
                operating_database_touched=False, source_documents_deleted=False), indent=2)+'\n')


if __name__ == '__main__': unittest.main()
