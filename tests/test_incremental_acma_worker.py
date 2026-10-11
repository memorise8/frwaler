"""Synthetic transport-layer requests through the unchanged ACMA parsing path."""
import contextlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

DSN=os.environ.get('INCREMENTAL_ROLLOUT_TEST_DSN')


@unittest.skipUnless(DSN,'requires a fresh owned PostgreSQL DB')
class AcmaWorkerTests(unittest.TestCase):
    def test_full_fallback_repeat_and_failed_transport_never_certify_incremental_baseline(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.sites import CRAWLERS
        from crawler.stealth_fetcher import StealthSession
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        from tests.test_incremental_acma import SITE,BASE,listing,detail
        self.assertTrue(urlsplit(DSN).path[1:].startswith('incremental_rollout_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'])
        db_pg.init_db(conn);init_delivery_schema(conn)
        cls=CRAWLERS[SITE];db_pg.upsert_site(conn,SITE,cls.site_name,cls.base_url)
        responses={BASE+'/publications':listing([3,2]),BASE+'/media-releases':listing([2,1]),
                   **{BASE+f'/doc-{i}':detail(i) for i in range(1,4)}}
        calls=[];failure=False;phases=[]

        def layer(name):
            def fetched(session,url):
                self.assertIn(url,responses);calls.append(dict(layer=name,url=url))
                if failure:return None,0,'controlled_transport_error'
                self.assertEqual(name,'curl_cffi');return responses[url],200,'ok'
            return fetched

        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres'}), \
                patch.object(StealthSession,'_fetch_curl_cffi',layer('curl_cffi')), \
                patch.object(StealthSession,'_fetch_playwright',layer('playwright')), \
                patch.object(StealthSession,'_fetch_requests',layer('requests')), \
                patch('requests.Session.request',side_effect=AssertionError('live network forbidden')), \
                patch('time.sleep'),contextlib.redirect_stdout(io.StringIO()):
            before=None
            for label in ('initial','repeat','transport_failure'):
                failure=label=='transport_failure';start=len(calls)
                jobs.enqueue_job(conn,SITE,mode='incremental')
                saved=worker.run_job(conn,jobs.claim_next_job(conn),delay=0)
                rows=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
                if before is not None:self.assertEqual(rows,before)
                before=rows;state=Store(conn,SITE).state()
                self.assertFalse(state['baseline_complete']);self.assertEqual(state['support'],'full_rescan_required')
                job=conn.execute('SELECT * FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()
                self.assertEqual(job['status'],'failed' if failure else 'done')
                if failure:self.assertIn('fetch_failed',str(dict(job)))
                reads=calls[start:];phases.append(dict(phase=label,saved_new=saved,rows=len(rows),
                    transport_layer_calls=len(reads),detail_calls=sum('/doc-' in r['url'] for r in reads),
                    list_calls=sum('/doc-' not in r['url'] for r in reads),job_status=job['status'],baseline_complete=False))
            self.assertEqual([p['saved_new'] for p in phases],[3,0,0])
            self.assertEqual([p['transport_layer_calls'] for p in phases],[5,5,9])
            self.assertEqual([p['detail_calls'] for p in phases],[3,3,0])
            self.assertEqual({r['post_number'] for r in rows},{'doc-1','doc-2','doc-3'})
            for row in rows:
                i=int(row['post_number'].rsplit('-',1)[1])
                self.assertEqual(row['title'],f'Research {i}')
                self.assertEqual(row['meta_url'],BASE+f'/doc-{i}')
                self.assertEqual(row['pdf_url'],BASE+f'/files/doc-{i}.pdf')
                self.assertEqual(str(row['published_date']),f'2026-10-0{i}')
                self.assertIn(f'abstract for document {i}.',row['abstract'])
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM crawl_jobs WHERE status='queued'").fetchone()['n'],0)
        output=os.environ.get('INCREMENTAL_ACMA_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists())
            Path(output).write_text(json.dumps(dict(status='passed',fixture_transport=True,
                synthetic_HTML_and_failure_responses=True,live_http_requests=0,database=urlsplit(DSN).path[1:],
                request_count_boundary='intercepted StealthSession native transport-layer invocations; not wire HTTP or browser subrequests',
                phases=phases,existing_rows_unchanged=True,global_source_complete=False,all_files_verified=False,
                source_documents_deleted=False,operating_database_touched=False),indent=2)+'\n')


if __name__=='__main__':unittest.main()
