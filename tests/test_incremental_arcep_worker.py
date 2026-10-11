"""Access-shape replay, not a claim about live native-route response equivalence."""
import contextlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

DSN=os.environ.get('INCREMENTAL_ROLLOUT_TEST_DSN')
SOURCE=os.environ.get('INCREMENTAL_ARCEP_SOURCE')


@unittest.skipUnless(DSN and SOURCE,'requires fresh owned PostgreSQL and retained ARCEP access response')
class ArcepWorkerTests(unittest.TestCase):
    def test_access_shape_failure_repeat_and_original_route_guard(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        from delivery.scripts.review_arcep_incremental import retained,SITE
        from tests.test_incremental_arcep import landing,response,CHASH
        self.assertTrue(urlsplit(DSN).path[1:].startswith('incremental_rollout_arcep_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'])
        db_pg.init_db(conn);init_delivery_schema(conn);cls=CRAWLERS[SITE];db_pg.upsert_site(conn,SITE,cls.site_name,cls.base_url)
        _,_,hold,raw=retained(SOURCE);g=cls.crawl.__globals__;calls=[];phases=[]
        native_filter=(g['_LIST_TMPL'].format(page=1)+'?tx_gspublication_publicationlist%5Bfilters%5D%5Btype%5D=15&cHash='+CHASH)
        def request(session,method,url,**kwargs):
            self.assertEqual(method.upper(),'GET');self.assertFalse(kwargs['allow_redirects']);calls.append(url)
            self.assertIn(url,(g['_MAIN_PUB_URL'],native_filter,hold['url']))
            return response(landing() if url==g['_MAIN_PUB_URL'] else raw.decode())
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres'}),patch('requests.Session.request',request), \
                patch.object(g['subprocess'],'run',side_effect=AssertionError('browser/subprocess forbidden')), \
                contextlib.redirect_stdout(io.StringIO()):
            for phase in ('initial','repeat'):
                start=len(calls);jobs.enqueue_job(conn,SITE,mode='incremental')
                saved=worker.run_job(conn,jobs.claim_next_job(conn),delay=0)
                job=conn.execute('SELECT * FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()
                self.assertEqual(job['status'],'failed');self.assertIn('authentication_failed',str(dict(job)))
                self.assertEqual(saved,0);self.assertEqual(len(calls)-start,2)
                self.assertEqual(conn.execute('SELECT count(*) AS n FROM documents').fetchone()['n'],0)
                state=Store(conn,SITE).state();self.assertFalse(state['baseline_complete']);self.assertEqual(state['support'],'full_rescan_required')
                phases.append(dict(phase=phase,saved_new=0,rows=0,request_method_invocations=2,detail_calls=0,
                    browser_navigations=0,job_status='failed',baseline_complete=False))
            c=cls(None,delay=0)
            try:
                self.assertIsNone(c._curl(hold['url']));self.assertEqual(c.delivery_source_failure,'authentication_failed')
            finally:c._session.close()
        output=os.environ.get('INCREMENTAL_ARCEP_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists())
            Path(output).write_text(json.dumps(dict(status='passed_expected_access_failure',fixture_transport=True,live_http_requests=0,
                retained_human_verification_body=True,retained_body_original_route=hold['url'],response_sha256=hold['source_sha256'],
                synthetic_hash_discovery_page=True,worker_response_shape_transplanted_to_native_filter=True,
                native_filter_live_response_equivalence_verified=False,additional_original_route_guard_replays=1,
                database=urlsplit(DSN).path[1:],phases=phases,baseline_complete=False,source_count=None,source_order=None,
                source_exhaustion_observed=False,global_source_complete=False,operating_database_touched=False,source_documents_deleted=False),indent=2)+'\n')


if __name__=='__main__':unittest.main()
