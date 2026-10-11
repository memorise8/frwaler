"""Default Adruk worker integration using a new owned PostgreSQL database."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock,patch
from urllib.parse import urlsplit

DSN=os.environ.get('INCREMENTAL_ROLLOUT_TEST_DSN')


@unittest.skipUnless(DSN,'requires a fresh incremental_rollout_ PostgreSQL DB')
class AdrukWorkerTests(unittest.TestCase):
    def test_default_worker_preserves_queue_and_avoids_repeat_details(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.incremental_adruk import AdrukAdapter
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        from tests.test_incremental_adruk import fixtures,response_for
        self.assertTrue(urlsplit(DSN).path.lstrip('/').startswith('incremental_rollout_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'],'must be a new empty DB')
        db_pg.init_db(conn);init_delivery_schema(conn)
        cls=CRAWLERS['adruk-org-news-publications'];db_pg.upsert_site(conn,cls.site_id,cls.site_name,cls.base_url)
        docs=fixtures();calls=[];phases=[]
        def request(c,url,**kw):
            calls.append(url);return response_for(url,docs,**kw)
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres','LIBERTREE_FULL_CHECK_DAYS':'0'}),patch.object(AdrukAdapter,'rows',2),patch.object(cls,'_request',request):
            baseline=None
            for label,limit in [('partial',3),('resume',None),('repeat',None)]:
                start=len(calls);jobs.enqueue_job(conn,cls.site_id,mode='incremental',limit_n=limit)
                saved=worker.run_job(conn,jobs.claim_next_job(conn),delay=0)
                rows=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
                state=Store(conn,cls.site_id).state()
                if baseline is None:baseline=rows;self.assertFalse(state.get('baseline_complete',False))
                else:self.assertEqual(rows[:3],baseline)
                self.assertEqual(conn.execute('SELECT status FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()['status'],'done')
                phases.append({'phase':label,'saved_new':saved,'requests':len(calls)-start,
                               'details':sum('/publication/' in u or '/article/' in u for u in calls[start:]),'rows':len(rows)})
            self.assertEqual([p['saved_new'] for p in phases],[3,9,0])
            self.assertEqual([p['requests'] for p in phases],[14,8,8])
            self.assertEqual([p['details'] for p in phases],[2,8,0])
            self.assertEqual([r['post_number'] for r in rows],list(reversed(docs['publications']))+list(reversed(docs['impact'])))
            self.assertIn('Administrative data',rows[0]['abstract'])
            self.assertEqual(rows[0]['published_date_raw'],'1 September 2026')
            self.assertEqual(rows[0]['listed_date_raw'],'1 September 2026')
            self.assertTrue(state['baseline_complete']);self.assertEqual(state['last_run']['stop_reason'],'known_boundary_plus_overlap')
            self.assertEqual(state['last_run']['save_attempts'],0)
        output=os.environ.get('INCREMENTAL_ADRUK_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists(),'preserve prior measurements')
            Path(output).write_text(json.dumps({'status':'passed','fixture_transport':True,'live_http_requests':0,
                'database':urlsplit(DSN).path[1:],'phases':phases,'existing_rows_unchanged':True,'source_documents_deleted':False},indent=2)+'\n')

if __name__=='__main__':unittest.main()
