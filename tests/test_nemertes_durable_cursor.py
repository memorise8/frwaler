"""Durable DSpace scope/page/item positions with source-drift and failure guards."""
import json,os,unittest
from unittest.mock import patch
from crawler.base_crawler import CrawlCancelled
from tests.test_nemertes_delivery_scopes import NemertesDeliveryScopes as _Scopes

class NemertesCursor(_Scopes):
    def backfill(self,cursor=None):
        c=self.crawler();self.module._PAGE_SIZE=2;c.delivery_mode='backfill';c.delivery_cursor=cursor;return c
    def test_partial_page_cursor_survives_new_crawler_and_all_five_scopes(self):
        cursor=None;observed=[]
        for expected in (3,3,2):
            c=self.backfill(cursor);self.assertEqual(c.crawl(limit=3),expected);self.assertIsNotNone(c.delivery_pending_cursor)
            observed.extend(x.kwargs['params']['scope'] for x in self.fetch.call_args_list if '/bundles' not in x.args[0]);cursor=json.loads(json.dumps(c.delivery_pending_cursor))
        self.assertEqual(self.count(),8);self.assertEqual(set(observed),set(self.module._SCOPES));self.assertTrue(c.delivery_exhausted)
        c=self.backfill(cursor);self.assertEqual(c.crawl(),0);self.assertTrue(c.delivery_exhausted)
    def test_cancel_keeps_committed_position_and_resumes_without_first_scope(self):
        c=self.backfill();c.delivery_should_cancel=lambda:self.count()>=3
        with self.assertRaises(CrawlCancelled):c.crawl()
        cursor=c.delivery_pending_cursor;self.assertIsNotNone(cursor);self.assertEqual(cursor['scope'],self.module._SCOPES[1]);self.assertEqual(cursor['offset'],1)
        c=self.backfill(cursor);self.assertEqual(c.crawl(),5)
        called=[x.kwargs['params']['scope'] for x in self.fetch.call_args_list if '/bundles' not in x.args[0]];self.assertNotIn(self.module._SCOPES[0],called)
    def test_failed_item_never_advances_checkpoint_past_missing_record(self):
        c=self.backfill();native=c._save_paper
        def save(paper):
            if paper['external_id'].startswith('00000003'):raise RuntimeError('controlled persistence failure')
            return native(paper)
        c._save_paper=save;self.assertEqual(c.crawl(),2);self.assertEqual(c.delivery_source_failure,'item_failed');cursor=c.delivery_pending_cursor;self.assertEqual(cursor['scope'],self.module._SCOPES[1]);self.assertEqual(cursor['page'],1);self.assertEqual(cursor['offset'],0)
        c=self.backfill(cursor);self.assertEqual(c.crawl(),6);self.assertTrue(c.delivery_exhausted)
    def test_changed_current_page_ids_do_not_silently_skip_new_item(self):
        c=self.backfill();c.crawl(limit=3);cursor=c.delivery_pending_cursor;c=self.backfill(cursor);native=self.fetch.side_effect
        def fetch(url,params=None):
            data=native(url,params)
            if '/bundles' not in url and params['scope']==cursor['scope'] and params['page']==cursor['page']:
                data['_embedded']['items'][0]['uuid']='99999999-0000-0000-0000-000000000000'
            return data
        self.fetch.side_effect=fetch;self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertIsNone(c.delivery_pending_cursor)
    def test_changed_source_total_requires_review_before_resume(self):
        c=self.backfill();c.crawl(limit=3);cursor=c.delivery_pending_cursor;c=self.backfill(cursor);native=self.fetch.side_effect
        def fetch(url,params=None):
            data=native(url,params)
            if '/bundles' not in url:data['page']['totalElements']+=1
            return data
        self.fetch.side_effect=fetch;self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response')
    def test_changed_previous_page_anchor_stops_before_current_page(self):
        c=self.backfill();c.crawl(limit=3);cursor=c.delivery_pending_cursor;c=self.backfill(cursor);native=self.fetch.side_effect
        def fetch(url,params=None):
            data=native(url,params)
            if '/bundles' not in url and params['scope']==cursor['scope'] and params['page']==cursor['page']-1:
                data['_embedded']['items'][0]['uuid']='99999999-0000-0000-0000-000000000000'
            return data
        self.fetch.side_effect=fetch;self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertIsNone(c.delivery_pending_cursor)
    def test_wall_budget_preserves_progress_for_next_backfill_job(self):
        c=self.backfill()
        with patch.object(self.module,'_CRAWL_BUDGET_SECONDS',0.5),patch.object(self.module.time,'time',side_effect=lambda:1.0 if self.count()>=2 else 0.0):
            self.assertEqual(c.crawl(),2)
        self.assertFalse(c.delivery_source_failure);self.assertFalse(c.delivery_exhausted)
        c=self.backfill(c.delivery_pending_cursor);self.assertEqual(c.crawl(),6);self.assertTrue(c.delivery_exhausted)
    def test_json_access_denial_does_not_retry(self):
        import requests
        c=self.backfill();response=requests.Response();response.status_code=403
        # Exercise the actual transport instead of the fixture response adapter.
        native=self.module.NemertesLibraryUpatrasGrBrowseCrawler._fetch_json
        with patch.object(c._session,'get',side_effect=requests.HTTPError(response=response)) as get:
            self.assertIsNone(native(c,'https://nemertes.library.upatras.gr/server/api/example'))
        self.assertEqual(get.call_count,1);self.assertEqual(c.delivery_source_failure,'authentication_failed')
    def test_scope_or_page_size_contract_change_rejects_cursor(self):
        c=self.backfill();c.crawl(limit=3);cursor=c.delivery_pending_cursor;c=self.backfill(cursor);self.module._PAGE_SIZE=3
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'configuration');self.fetch.assert_not_called()
    def test_full_mode_ignores_backfill_cursor_and_preserves_native_replay(self):
        c=self.backfill();c.crawl(limit=3);cursor=c.delivery_pending_cursor;c=self.crawler();c.delivery_cursor=cursor
        self.assertEqual(c.crawl(),5);self.assertIsNone(c.delivery_pending_cursor)
        called=[x.kwargs['params']['scope'] for x in self.fetch.call_args_list if '/bundles' not in x.args[0]];self.assertEqual(called[0],self.module._SCOPES[0])
    def test_bodyless_item_is_unresolved_in_backfill_not_completed(self):
        c=self.backfill();native=self.fetch.side_effect
        def fetch(url,params=None):
            data=native(url,params)
            if '/bundles' in url and '/00000003-' in url:return {'page':{'totalElements':0},'_embedded':{'bundles':[]}}
            if '/bundles' not in url:
                for item in data.get('_embedded',{}).get('items',[]):
                    if item['uuid'].startswith('00000003'):item['metadata']['dc.description.abstract']=[]
            return data
        self.fetch.side_effect=fetch;self.assertEqual(c.crawl(),2);self.assertEqual(c.delivery_source_failure,'item_failed');self.assertFalse(c.delivery_exhausted)

@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class NemertesCursorPostgres(NemertesCursor):BACKEND='postgres'

@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class NemertesCursorWorker(unittest.TestCase):
    def test_actual_worker_persists_midpage_cancel_and_auto_chain_to_completion(self):
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        case=NemertesCursorPostgres(methodName='test_partial_page_cursor_survives_new_crawler_and_all_five_scopes')
        try:
            case.setUp();case.module._PAGE_SIZE=2;init_delivery_schema(case.conn)
            # Factory is the actual class contract, not a lambda hiding cursor version.
            class ObservedFactory:
                DELIVERY_CURSOR_VERSION=case.module.NemertesLibraryUpatrasGrBrowseCrawler.DELIVERY_CURSOR_VERSION
                DELIVERY_ORDER='arbitrary'
                def __new__(cls,**kwargs):return case.crawler()
            jid=jobs.enqueue_job(case.conn,case.SITE,mode='backfill',limit_n=3)
            self.assertEqual(worker.run_job(case.conn,jobs.claim_next_job(case.conn),{case.SITE:ObservedFactory},delay=0),3)
            saved_cursor=jobs.load_cursor(case.conn,case.SITE);self.assertEqual(saved_cursor['offset'],1)
            job=jobs.claim_next_job(case.conn);self.assertIsNotNone(job)
            before=case.count()
            def cancel():
                if case.count()>=before+1:jobs.cancel_job(case.conn,job['id']);return True
                return False
            self.assertEqual(worker.run_job(case.conn,job,{case.SITE:ObservedFactory},delay=0,should_cancel=cancel),1)
            self.assertIsNone(jobs.claim_next_job(case.conn));self.assertEqual(jobs.job_detail(case.conn,job['id'])['job']['status'],'cancelled')
            persisted=jobs.load_cursor(case.conn,case.SITE);self.assertIsNotNone(persisted)
            jobs.enqueue_job(case.conn,case.SITE,mode='backfill',limit_n=3)
            counts=[]
            for _ in range(8):
                job=jobs.claim_next_job(case.conn)
                if not job:break
                counts.append(worker.run_job(case.conn,job,{case.SITE:ObservedFactory},delay=0))
            self.assertEqual(counts,[3,1]);self.assertEqual(case.count(),8)
            progress=case.conn.execute('SELECT * FROM crawl_site_progress WHERE site_id=%s',(case.SITE,)).fetchone();self.assertIsNotNone(progress['completed_at'])
        finally:case.doCleanups()
