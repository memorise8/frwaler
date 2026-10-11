import os
import unittest
from unittest.mock import patch, Mock

TEST_PG_DSN=os.environ.get('TEST_PG_DSN')


@unittest.skipUnless(TEST_PG_DSN,'set TEST_PG_DSN to an isolated DB')
class SharedWorkerTests(unittest.TestCase):
    def setUp(self):
        from crawler import db_pg
        from delivery.db.schema import init_delivery_schema
        self.env=patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres','LIBERTREE_FULL_CHECK_DAYS':'0'})
        self.env.start()
        self.conn=db_pg.open_db(TEST_PG_DSN)
        self.conn.execute('DROP SCHEMA public CASCADE');self.conn.execute('CREATE SCHEMA public');self.conn.commit()
        db_pg.init_db(self.conn);init_delivery_schema(self.conn)
        db_pg.upsert_site(self.conn,'doaj-org-search','DOAJ test','https://doaj.org')

    def tearDown(self):
        self.conn.close();self.env.stop()

    def test_default_worker_persists_baseline_only_after_queue_drains(self):
        from crawler.sites import CRAWLERS
        from crawler.incremental import Store
        from delivery.worker import jobs,worker
        cls=CRAWLERS['doaj-org-search']
        data={'total':6,'results':[{'id':str(i),'created_date':f'2026-01-{i:02}T00:00:00Z',
                                  'bibjson':{'title':str(i)}} for i in range(6,0,-1)]}
        response=Mock();response.json.return_value=data
        with patch.object(cls,'_request',return_value=response) as fetch:
            jid=jobs.enqueue_job(self.conn,cls.site_id,mode='incremental',limit_n=2)
            job=jobs.claim_next_job(self.conn)
            self.assertEqual(worker.run_job(self.conn,job,delay=0),2)
            state=Store(self.conn,cls.site_id).state()
            self.assertFalse(state.get('baseline_complete',False))
            self.assertEqual(state['phase'],'deliver')
            self.assertEqual([r['post_number'] for r in self.conn.execute('SELECT post_number FROM documents ORDER BY seq_id')],['1','2'])
            self.assertEqual(fetch.call_count,1)
            jid=jobs.enqueue_job(self.conn,cls.site_id,mode='incremental')
            self.assertEqual(worker.run_job(self.conn,jobs.claim_next_job(self.conn),delay=0),4)
            self.assertEqual(fetch.call_count,1) # durable queue, no list replay
            state=Store(self.conn,cls.site_id).state()
            self.assertTrue(state['baseline_complete'])
            self.assertEqual(state['baseline']['id'],'6')
            self.assertEqual(state['order'],'newest_first')
            self.assertEqual(self.conn.execute("SELECT count(*) AS n FROM crawl_incremental_pending").fetchone()['n'],0)
            jobs.enqueue_job(self.conn,cls.site_id,mode='incremental')
            self.assertEqual(worker.run_job(self.conn,jobs.claim_next_job(self.conn),delay=0),0)
            self.assertEqual(fetch.call_count,2)
            self.assertEqual(self.conn.execute('SELECT status FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()['status'],'done')
            self.assertEqual(Store(self.conn,cls.site_id).state()['last_run']['save_attempts'],0)

    def test_legacy_known_boundary_never_certifies_backfill(self):
        from crawler.base_crawler import BaseCrawler,CrawlUpToDate
        from delivery.worker import jobs,worker
        class Fake(BaseCrawler):
            site_id='legacy';site_name='Legacy';base_url='https://example.invalid'
            def crawl(self,limit=None):
                self._advance_cursor({'page':2},1)
                raise CrawlUpToDate()
        jobs.enqueue_job(self.conn,'legacy',mode='backfill')
        worker.run_job(self.conn,jobs.claim_next_job(self.conn),{'legacy':Fake},delay=0)
        row=self.conn.execute("SELECT completed_at FROM crawl_site_progress WHERE site_id='legacy'").fetchone()
        self.assertIsNone(row['completed_at'])

if __name__=='__main__':unittest.main()
