"""Durable native backfill, source-prefix validation, and real DB/worker lifecycle."""
import json,os,unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit
from crawler.base_crawler import CrawlCancelled
from tests.test_delivery_site_new_counts import SiteContract,InterruptedAfterCommit
from tests.test_open_science_delivery_scopes import OpenScienceDeliveryScopes

class OpenScienceCursor(SiteContract,unittest.TestCase):
    SITE=OpenScienceDeliveryScopes.SITE
    ORIGINAL=OpenScienceDeliveryScopes.ORIGINAL;ADDITIONAL=OpenScienceDeliveryScopes.ADDITIONAL
    item=OpenScienceDeliveryScopes.item
    def crawler(self):
        c=OpenScienceDeliveryScopes.crawler(self);self.addCleanup(c._session.close);return c
    def backfill(self,cursor=None):
        c=self.crawler();c.delivery_mode='backfill';c.delivery_cursor=cursor;return c
    def bundle_requests(self):return sum('/bundles?' in x.args[0] for x in self.fetch.call_args_list)

    def test_three_three_two_cursor_crosses_scope_overlap_without_refetching_bundles(self):
        cursor=None
        for expected in (3,3,2):
            c=self.backfill(cursor);self.assertEqual(c.crawl(limit=3),expected);self.assertEqual(self.bundle_requests(),expected)
            cursor=json.loads(json.dumps(c.delivery_pending_cursor));self.assertTrue(all(not isinstance(v,(list,dict)) for v in cursor.values()))
        self.assertTrue(c.delivery_exhausted);before=self.identities();c=self.backfill(cursor)
        self.assertEqual(c.crawl(),0);self.assertTrue(c.delivery_exhausted);self.assertEqual(self.bundle_requests(),0);self.assertEqual(before,self.identities())

    def test_cancel_after_commit_keeps_offset_and_resumes(self):
        c=self.backfill();c.delivery_should_cancel=lambda:self.count()>=3
        with self.assertRaises(CrawlCancelled):c.crawl()
        cursor=c.delivery_pending_cursor;self.assertEqual((cursor['scope'],cursor['page'],cursor['offset']),(self.ORIGINAL,0,3))
        before=self.identities();c=self.backfill(cursor);self.assertEqual(c.crawl(),5);self.assertEqual(self.bundle_requests(),5);self.assertTrue(all(self.identities()[k]==v for k,v in before.items()));self.assertTrue(c.delivery_exhausted)

    def test_process_interruption_between_commit_and_checkpoint_reconciles_known_row(self):
        c=self.backfill();save=c._save_paper
        def interrupt(paper):
            save(paper)
            if self.count()==2:raise InterruptedAfterCommit()
        c._save_paper=interrupt
        with self.assertRaises(InterruptedAfterCommit):c.crawl()
        self.assertEqual(c.delivery_pending_cursor['offset'],1);before=self.identities();c=self.backfill(c.delivery_pending_cursor)
        self.assertEqual(c.crawl(),6);self.assertTrue(c.delivery_exhausted);self.assertTrue(all(self.identities()[k]==v for k,v in before.items()));self.assertEqual(self.bundle_requests(),6)

    def test_body_gap_holds_position_until_source_body_is_available(self):
        self.excluded={4};c=self.backfill();self.assertEqual(c.crawl(),3);cursor=c.delivery_pending_cursor
        self.assertEqual((cursor['scope'],cursor['page'],cursor['offset']),(self.ADDITIONAL,1,0));self.assertEqual(c.delivery_source_failure,'item_failed');self.assertFalse(c.delivery_exhausted)
        c=self.backfill(cursor);c._curl_get.return_value='<meta name="description" content="'+'Recovered source scientific body. '*10+'">'
        self.assertEqual(c.crawl(),5);self.assertTrue(c.delivery_exhausted)

    def test_save_failure_does_not_advance_past_unsaved_item(self):
        c=self.backfill();save=c._save_paper
        def fail(paper):
            if paper['external_id'].startswith('00000004'):raise RuntimeError('controlled failed save')
            return save(paper)
        c._save_paper=fail;self.assertEqual(c.crawl(),3);cursor=c.delivery_pending_cursor;self.assertEqual(cursor['offset'],0)
        c=self.backfill(cursor);self.assertEqual(c.crawl(),5);self.assertTrue(c.delivery_exhausted)

    def test_changed_nonadjacent_source_prefix_is_rejected_before_new_saves(self):
        c=self.backfill();c.crawl(limit=6);cursor=c.delivery_pending_cursor;before=self.identities();c=self.backfill(cursor);original=self.fetch.side_effect
        def fetch(url,context='request'):
            data=original(url,context)
            if '/discover/search/objects' in url and parse_qs(urlsplit(url).query)['scope']==[self.ORIGINAL]:
                data['_embedded']['searchResult']['_embedded']['objects'][0]['_embedded']['indexableObject']['metadata']['dc.title'][0]['value']='Changed original first-page title'
            return data
        self.fetch.side_effect=fetch;self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertEqual(before,self.identities());self.assertEqual(self.bundle_requests(),0)

    def test_changed_current_page_is_rejected(self):
        c=self.backfill();c.crawl(limit=4);cursor=c.delivery_pending_cursor;c=self.backfill(cursor);original=self.fetch.side_effect
        def fetch(url,context='request'):
            data=original(url,context)
            if '/discover/search/objects' in url and parse_qs(urlsplit(url).query)['page']==['1']:
                data['_embedded']['searchResult']['_embedded']['objects'][-1]['_embedded']['indexableObject']['name']='Changed source name'
            return data
        self.fetch.side_effect=fetch;self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertEqual(self.count(),4)

    def test_missing_previously_committed_row_is_not_silently_skipped(self):
        c=self.backfill();c.crawl(limit=6);cursor=c.delivery_pending_cursor
        self.conn.execute("DELETE FROM documents WHERE post_number='00000001-0000-0000-0000-000000000000'");self.conn.commit()
        c=self.backfill(cursor);self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertEqual(self.count(),5);self.assertEqual(self.bundle_requests(),0)

    def test_existing_identity_collision_is_held_without_merge(self):
        c=self.backfill();c.crawl(limit=1);cursor=c.delivery_pending_cursor;mark='%s' if self.BACKEND=='postgres' else '?'
        self.conn.execute(f'UPDATE documents SET meta_url={mark}',('https://open-science.canada.ca/other-existing-identity',));self.conn.commit()
        c=self.backfill(cursor);self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertEqual(self.count(),1)

    def test_malformed_cursor_or_contract_mismatch_makes_no_requests(self):
        c=self.backfill();c.crawl(limit=2);cursor=c.delivery_pending_cursor
        for mutation in ({'version':True},{'version':2},{'page':False},{'offset':99},{'page_hash':''},{'prefix_hash':'bad'},{'scope':'unknown'},{'total':99}):
            c=self.backfill(dict(cursor,**mutation));self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'configuration');self.fetch.assert_not_called()
        c=self.backfill(cursor);c.PAGE_SIZE=4;self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'configuration');self.fetch.assert_not_called()

    def test_wall_checkpoint_and_full_mode_does_not_use_or_overwrite_cursor(self):
        c=self.backfill();c.BACKFILL_WALL_SECONDS=.5
        with patch.object(self.module.time,'monotonic',side_effect=lambda:1 if self.count()>=3 else 0):self.assertEqual(c.crawl(),3)
        self.assertIsNone(c.delivery_source_failure);cursor=c.delivery_pending_cursor
        c=self.crawler();c.delivery_cursor=cursor;self.assertEqual(c.crawl(),5);self.assertIsNone(c.delivery_pending_cursor)

    def test_structural_entities_advance_cursor_without_document_rows(self):
        c=self.backfill();original=self.fetch.side_effect
        def fetch(url,context='request'):
            data=original(url,context)
            if '/discover/search/objects' in url and parse_qs(urlsplit(url).query)['scope']==[self.ADDITIONAL]:
                result=data['_embedded']['searchResult'];result['page']['totalElements']=9
                if result['page']['number']==2:result['_embedded']['objects'].append({'_embedded':{'indexableObject':{'uuid':'00000009-0000-0000-0000-000000000000','type':'collection','name':'Navigation'}}})
            return data
        self.fetch.side_effect=fetch;self.assertEqual(c.crawl(),8);self.assertTrue(c.delivery_exhausted);self.assertEqual(c.delivery_pending_cursor['offset'],3);self.assertEqual(self.bundle_requests(),8)

@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires disposable owned PostgreSQL')
class OpenScienceCursorPostgres(OpenScienceCursor):BACKEND='postgres'

@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires disposable owned PostgreSQL')
class OpenScienceCursorWorker(unittest.TestCase):
    def test_worker_persists_cursor_cancellation_and_auto_chain(self):
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        case=OpenScienceCursorPostgres(methodName='test_three_three_two_cursor_crosses_scope_overlap_without_refetching_bundles')
        try:
            case.setUp();init_delivery_schema(case.conn)
            class Factory:
                DELIVERY_CURSOR_VERSION=1;DELIVERY_ORDER='arbitrary'
                def __new__(cls,**kw):return case.crawler()
            jobs.enqueue_job(case.conn,case.SITE,mode='backfill',limit_n=3)
            self.assertEqual(worker.run_job(case.conn,jobs.claim_next_job(case.conn),{case.SITE:Factory},delay=0),3);job=jobs.claim_next_job(case.conn)
            def cancel():
                if case.count()>=4:jobs.cancel_job(case.conn,job['id']);return True
                return False
            self.assertEqual(worker.run_job(case.conn,job,{case.SITE:Factory},delay=0,should_cancel=cancel),1);self.assertEqual(jobs.job_detail(case.conn,job['id'])['job']['status'],'cancelled');self.assertIsNone(jobs.claim_next_job(case.conn))
            jobs.enqueue_job(case.conn,case.SITE,mode='backfill',limit_n=3);counts=[]
            for _ in range(8):
                job=jobs.claim_next_job(case.conn)
                if not job:break
                counts.append(worker.run_job(case.conn,job,{case.SITE:Factory},delay=0))
            self.assertEqual(counts,[3,1]);self.assertEqual(case.count(),8);self.assertIsNotNone(case.conn.execute('SELECT completed_at FROM crawl_site_progress').fetchone()['completed_at'])
        finally:case.doCleanups()
