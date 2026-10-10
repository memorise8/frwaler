"""ENS durable resume over stored, repeated and explicitly excluded source items."""
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crawler.base_crawler import CrawlCancelled
from tests import test_europe_press_counts as fixtures
from tests.test_delivery_site_new_counts import InterruptedAfterCommit


class EnsCursorTests(unittest.TestCase):
    BACKEND = 'sqlite'

    def setUp(self):
        self.case = fixtures.EnsLyonCounts(methodName='test_zero_limit_performs_no_fetch')
        self.case.BACKEND = self.BACKEND
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.conn = self.case.conn

    def crawler(self, cursor=None):
        c = self.case.crawler()
        c.delivery_mode = 'backfill'
        c.delivery_cursor = cursor
        old = self.case.fetch.side_effect
        def fetch(url, **kw):
            raw = old(url, **kw)
            if '/report-' not in url:
                raw = '<h1>Communiqués de presse</h1><div class="view-communiques-de-presse">'+raw+'</div>'
            return raw
        self.case.fetch.side_effect = fetch
        return c

    def test_partial_pages_resume_verifies_lists_without_refetching_stored_details(self):
        cursor = None
        for before, expected in ((0,3),(3,3),(6,2),(8,0)):
            c = self.crawler(cursor);self.assertEqual(c.crawl(limit=3),expected)
            cursor = c.delivery_pending_cursor
            details = [call.args[0] for call in self.case.fetch.call_args_list if '/report-' in call.args[0]]
            self.assertEqual(details,[c.base_url+f'/report-{n}.html' for n in range(before+1,before+expected+1)])
        self.assertEqual((cursor['page'],cursor['offset']), (1,3))
        self.assertTrue(c.delivery_exhausted);self.assertEqual(self.case.count(),8)

    def test_cancel_after_save_preserves_committed_offset(self):
        c = self.crawler();c.delivery_should_cancel=lambda:self.case.count()>=3
        with self.assertRaises(CrawlCancelled):c.crawl()
        self.assertEqual(c.delivery_pending_cursor['offset'],3)
        c=self.crawler(c.delivery_pending_cursor);self.assertEqual(c.crawl(),5);self.assertTrue(c.delivery_exhausted)

    def test_commit_before_checkpoint_replays_overlap_without_duplicate(self):
        c=self.crawler()
        def committed(seq):
            if self.case.count()==3:raise InterruptedAfterCommit()
        c.delivery_document_saved=committed
        with self.assertRaises(InterruptedAfterCommit):c.crawl()
        self.assertEqual(c.delivery_pending_cursor['offset'],2)
        self.assertEqual(self.crawler(c.delivery_pending_cursor).crawl(),5);self.assertEqual(self.case.count(),8)

    def test_short_body_exclusion_is_anchored_and_refetched_on_resume(self):
        self.case.excluded={2};c=self.crawler();self.assertEqual(c.crawl(limit=3),3)
        cursor=c.delivery_pending_cursor;self.assertEqual(cursor['offset'],4)
        self.assertEqual(set(json.loads(cursor['exclusions_json'])),{'2'})
        c=self.crawler(cursor);self.assertEqual(c.crawl(),4);self.assertTrue(c.delivery_exhausted)
        fetched=[x.args[0] for x in self.case.fetch.call_args_list]
        self.assertIn(c.base_url+'/report-2.html',fetched)
        self.assertNotIn(c.base_url+'/report-1.html',fetched);self.assertEqual(self.case.count(),7)

    def test_newly_eligible_excluded_prefix_is_held_without_saving_or_advancing(self):
        self.case.excluded={2};c=self.crawler();c.crawl(limit=3);cursor=c.delivery_pending_cursor
        self.case.excluded=set();c=self.crawler(cursor);self.assertEqual(c.crawl(),0)
        self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertIsNone(c.delivery_pending_cursor)
        self.assertEqual(self.case.count(),3)

    def test_short_body_fallback_change_invalidates_exclusion_fingerprint(self):
        self.case.excluded={2};c=self.crawler();old=self.case.fetch.side_effect
        self.case.fetch.side_effect=lambda url,**kw:old(url,**kw)+'<main><div class="field--name-bp-text">Video</div></main>' if url.endswith('/report-2.html') else old(url,**kw)
        self.assertEqual(c.crawl(limit=3),3);cursor=c.delivery_pending_cursor
        c=self.crawler(cursor);old=self.case.fetch.side_effect
        self.case.fetch.side_effect=lambda url,**kw:old(url,**kw)+'<main><div class="field--name-bp-text">Updated video</div></main>' if url.endswith('/report-2.html') else old(url,**kw)
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response')

    def test_exclusion_fingerprint_tampering_and_extra_entries_are_held(self):
        self.case.excluded={2};c=self.crawler();c.crawl(limit=3)
        for exclusions in ({'2':'0'*64},{'2':json.loads(c.delivery_pending_cursor['exclusions_json'])['2'],'999':'0'*64}):
            cursor=dict(c.delivery_pending_cursor,exclusions_json=json.dumps(exclusions))
            check=self.crawler(cursor);self.assertEqual(check.crawl(),0);self.assertEqual(check.delivery_source_failure,'invalid_response')

    def test_valid_duplicate_membership_across_pages_is_not_a_second_document(self):
        c=self.crawler();old=self.case.fetch.side_effect
        first=c._make_soup(old(c.LIST_URL+'?page=0')).select('article')[0]
        self.case.fetch.side_effect=lambda url,**kw:old(url,**kw).replace('</div>',str(first)+'</div>',1) if url.endswith('?page=1') else old(url,**kw)
        self.assertEqual(c.crawl(),8);self.assertTrue(c.delivery_exhausted);self.assertEqual(c.delivery_cursor_items_done,8)
        self.assertEqual(c.delivery_pending_cursor['offset'],4)

    def test_same_key_with_different_URL_and_repeated_whole_page_are_held(self):
        for kind in ('conflict','repeat'):
            # Each subcase starts from the same accepted first five rows.
            c=self.crawler();old=self.case.fetch.side_effect;first=old(c.LIST_URL+'?page=0')
            def fetch(url,**kw):
                raw=old(url,**kw)
                if url.endswith('?page=1'):
                    return first if kind=='repeat' else raw.replace('data-history-node-id="6"','data-history-node-id="1"')
                return raw
            self.case.fetch.side_effect=fetch;c.crawl()
            self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertFalse(c.delivery_exhausted)
            self.assertEqual(self.case.count(),5)

    def test_previous_page_and_stored_row_drift_reject_resume(self):
        c=self.crawler();c.crawl(limit=6);cursor=c.delivery_pending_cursor
        c=self.crawler(cursor);old=self.case.fetch.side_effect
        self.case.fetch.side_effect=lambda url,**kw:old(url,**kw).replace('Report 1','Changed source')
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response')
        self.conn.execute("UPDATE documents SET abstract='Changed stored body' WHERE seq_id=1");self.conn.commit()
        c=self.crawler(cursor);self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response')

    def test_removed_row_rejects_resume_while_file_flags_do_not(self):
        c=self.crawler();c.crawl(limit=3);cursor=c.delivery_pending_cursor
        self.conn.execute('UPDATE documents SET pdf_downloaded=1,text_extracted=1 WHERE seq_id=1');self.conn.commit()
        self.assertEqual(self.crawler(cursor).crawl(limit=1),1)
        self.conn.execute('DELETE FROM documents WHERE seq_id=1');self.conn.commit()
        c=self.crawler(cursor);self.assertEqual(c.crawl(),0);self.assertFalse(c.delivery_exhausted)

    def test_empty_error_page_does_not_complete_but_valid_empty_boundary_does(self):
        for html in ('<h1>Error</h1>','<h1>Communiqués de presse</h1>'):
            c=self.crawler();self.case.fetch.side_effect=None;self.case.fetch.return_value=html
            self.assertEqual(c.crawl(),0);self.assertFalse(c.delivery_exhausted)
        c=self.crawler();self.assertEqual(c.crawl(),8);self.assertTrue(c.delivery_exhausted)

    def test_empty_page_after_promised_next_page_is_incomplete(self):
        c=self.crawler();old=self.case.fetch.side_effect
        self.case.fetch.side_effect=lambda url,**kw:old(c.LIST_URL+'?page=2') if url.endswith('?page=1') else old(url,**kw)
        self.assertEqual(c.crawl(),5);self.assertFalse(c.delivery_exhausted)
        self.assertEqual(c.delivery_source_failure,'invalid_response')

    def test_next_link_must_be_adjacent_and_on_source_host(self):
        c=self.crawler();old=self.case.fetch.side_effect
        self.case.fetch.side_effect=lambda url,**kw:old(url,**kw).replace('href="?page=1"','href="https://foreign.invalid/?page=1"')
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response')

    def test_failed_detail_holds_offset_before_that_document(self):
        c=self.crawler();old=self.case.fetch.side_effect
        self.case.fetch.side_effect=lambda url,**kw:None if url.endswith('/report-4.html') else old(url,**kw)
        self.assertEqual(c.crawl(),3);self.assertEqual(c.delivery_pending_cursor['offset'],3)
        self.assertEqual(c.delivery_source_failure,'item_failed');self.assertFalse(c.delivery_exhausted)

    def test_missing_title_is_a_failure_not_an_exclusion(self):
        c=self.crawler();old=self.case.fetch.side_effect
        self.case.fetch.side_effect=lambda url,**kw:'<main>Bad response</main>' if url.endswith('/report-4.html') else old(url,**kw)
        self.assertEqual(c.crawl(),3);self.assertEqual(c.delivery_pending_cursor['offset'],3)
        self.assertEqual(c.delivery_source_failure,'item_failed')

    def test_error_heading_and_wrong_source_identity_are_not_short_body_exclusions(self):
        for html in ('<h1>Server error</h1>', '<article data-history-node-id="999"><h1>Other article</h1></article>'):
            c=self.crawler();old=self.case.fetch.side_effect
            self.case.fetch.side_effect=lambda url,**kw:html if url.endswith('/report-4.html') else old(url,**kw)
            c.crawl();self.assertEqual(c.delivery_pending_cursor['offset'],3)
            self.assertEqual(c.delivery_source_failure,'item_failed')
            self.assertEqual(json.loads(c.delivery_pending_cursor['exclusions_json']),{})

    def test_existing_key_URL_mismatch_holds_without_inserting(self):
        c=self.crawler();c.crawl(limit=3)
        self.conn.execute("UPDATE documents SET meta_url='https://changed.invalid/' WHERE seq_id=1");self.conn.commit()
        c=self.crawler();self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'item_failed')
        self.assertEqual(self.case.count(),3)

    def test_page_cap_and_zero_budget_never_report_completion(self):
        c=self.crawler();c.MAX_PAGES=1;self.assertEqual(c.crawl(),5)
        self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertFalse(c.delivery_exhausted)
        c=self.crawler(c.delivery_pending_cursor);c.WALL_CLOCK_LIMIT=0
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response')

    def test_invalid_cursor_and_zero_limit_do_not_fetch(self):
        c=self.crawler({'version':1,'page':False});self.assertEqual(c.crawl(),0);self.case.fetch.assert_not_called()
        c=self.crawler();self.assertEqual(c.crawl(limit=0),0);self.case.fetch.assert_not_called()

    def test_incremental_does_not_mutate_backfill_checkpoint(self):
        c=self.crawler();c.crawl(limit=3);cursor=c.delivery_pending_cursor
        c=self.crawler(cursor);c.delivery_mode='incremental';self.assertEqual(c.crawl(),5)
        self.assertIsNone(c.delivery_pending_cursor)

    def test_transport_rejects_error_status_partial_response_and_insecure_TLS(self):
        for status,code,reason in ((403,0,'authentication_failed'),(429,0,'rate_limited'),(503,0,'rate_limited'),(200,28,None),(500,0,None)):
            c=self.crawler();result=SimpleNamespace(stdout=b'<main>Error</main>\n'+str(status).encode(),stderr=b'',returncode=code)
            with patch.object(self.case.module.subprocess,'run',return_value=result) as run:
                self.assertIsNone(self.case.module.EnsLyonFrLecoleCrawler._curl_get(c,c.base_url))
            self.assertEqual(c.delivery_source_failure,reason)
            self.assertNotIn('-skL',run.call_args.args[0]);self.assertNotIn('--insecure',run.call_args.args[0])
            self.assertEqual(run.call_count,3 if status==500 else 1)


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class EnsCursorPostgresTests(EnsCursorTests):
    BACKEND='postgres'

    def test_worker_persists_cancel_resume_and_source_exclusion_completion(self):
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        init_delivery_schema(self.conn);self.case.excluded={2}
        for index,(limit,cancel,wanted) in enumerate(((3,None,3),(3,2,2),(3,None,2),(None,None,0))):
            c=self.crawler();before=self.case.count()
            if index in (0,2,3):jobs.enqueue_job(self.conn,self.case.SITE,mode='backfill',limit_n=limit)
            job=jobs.claim_next_job(self.conn);self.assertIsNotNone(job)
            def cancelled():
                if cancel and self.case.count()>=before+cancel:jobs.cancel_job(self.conn,job['id']);return True
                return False
            self.assertEqual(worker.run_job(self.conn,job,{self.case.SITE:lambda **kw:c},delay=0,should_cancel=cancelled),wanted)
            state=jobs.job_detail(self.conn,job['id'])['job']
            self.assertEqual((state['saved_count'],state['status']),(wanted,'cancelled' if cancel else 'done'))
        progress=self.conn.execute('SELECT * FROM crawl_site_progress').fetchone()
        self.assertEqual(progress['items_done'],8);self.assertIsNotNone(progress['completed_at'])
        self.assertEqual(set(json.loads(progress['cursor']['exclusions_json'])),{'2'})
