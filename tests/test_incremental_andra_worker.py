"""Replay observed native-route access refusal in an owned worker database."""
import contextlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

DSN=os.environ.get('INCREMENTAL_ROLLOUT_TEST_DSN')
SOURCE=os.environ.get('INCREMENTAL_ANDRA_SOURCE')


@unittest.skipUnless(DSN and SOURCE,'requires fresh owned PostgreSQL and retained ANDRA responses')
class AndraWorkerTests(unittest.TestCase):
    def test_repeated_access_refusal_has_no_baseline_or_followup_requests(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        from delivery.scripts.review_andra_incremental import retained,SITE
        self.assertTrue(urlsplit(DSN).path[1:].startswith('incremental_rollout_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'])
        db_pg.init_db(conn);init_delivery_schema(conn)
        cls=CRAWLERS[SITE];db_pg.upsert_site(conn,SITE,cls.site_name,cls.base_url)
        responses=retained(SOURCE);url=cls.crawl.__globals__['_START_URL'];calls=[];phases=[]
        def curl(cmd,**kwargs):
            self.assertEqual(cmd[0],'curl');self.assertEqual(cmd[-1],url);calls.append(url)
            return SimpleNamespace(returncode=0,stdout=b'HTTP/2 200\r\nContent-Type: text/html\r\n\r\n'+responses[url]['raw'],stderr=b'')
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres'}), \
                patch.object(cls.crawl.__globals__['subprocess'],'run',curl), \
                patch('requests.Session.request',side_effect=AssertionError('live HTTP forbidden')), \
                contextlib.redirect_stdout(io.StringIO()):
            for phase in ('initial','repeat'):
                start=len(calls);jobs.enqueue_job(conn,SITE,mode='incremental')
                saved=worker.run_job(conn,jobs.claim_next_job(conn),delay=0)
                job=conn.execute('SELECT * FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()
                self.assertEqual(job['status'],'failed');self.assertIn('authentication_failed',str(dict(job)))
                self.assertEqual(saved,0);self.assertEqual(len(calls)-start,1)
                self.assertEqual(conn.execute('SELECT count(*) AS n FROM documents').fetchone()['n'],0)
                self.assertEqual(conn.execute("SELECT count(*) AS n FROM crawl_jobs WHERE status='queued'").fetchone()['n'],0)
                state=Store(conn,SITE).state();self.assertFalse(state['baseline_complete'])
                self.assertEqual(state['support'],'full_rescan_required')
                phases.append(dict(phase=phase,saved_new=0,rows=0,native_curl_command_calls=1,
                    challenge_redirect_calls=0,ajax_calls=0,pdf_calls=0,job_status='failed',baseline_complete=False))
            # Exercise all three actual retained response shapes on their own
            # original routes without pretending the native crawler covers them.
            for original,r in responses.items():
                c=cls(None,delay=0)
                try:
                    def original_curl(cmd,**kwargs):
                        self.assertEqual(cmd[-1],original)
                        return SimpleNamespace(returncode=0,stdout=r['raw'],stderr=b'')
                    with patch.object(cls.crawl.__globals__['subprocess'],'run',original_curl):
                        self.assertIsNone(c._curl(original))
                    self.assertEqual(c.delivery_source_failure,'authentication_failed')
                finally:c._session.close()
        output=os.environ.get('INCREMENTAL_ANDRA_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists())
            Path(output).write_text(json.dumps(dict(status='passed_expected_access_failure',fixture_transport=True,
                live_http_requests=0,retained_actual_native_response_bodies=1,retained_delivered_response_shapes_checked=3,
                direct_source_guard_replays=3,synthetic_document_bodies=0,database=urlsplit(DSN).path[1:],phases=phases,
                native_response_sha256=responses[url]['sha256'],request_count_boundary='native curl subprocess invocations',
                baseline_complete=False,source_count=None,source_order=None,global_source_complete=False,
                original_three_category_scope_supported=False,operating_database_touched=False,source_documents_deleted=False),indent=2)+'\n')


if __name__=='__main__':unittest.main()
