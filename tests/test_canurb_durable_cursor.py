"""CANURB native persistence, interrupted saves, source drift and transport errors."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crawler.base_crawler import CrawlCancelled
from tests import test_canurb_linked_publications as fixtures
from tests.test_delivery_site_new_counts import InterruptedAfterCommit


class CanurbCursorTests(unittest.TestCase):
    BACKEND = 'sqlite'

    def setUp(self):
        self.case = fixtures.LinkedPublicationCounts(methodName='test_repeat_three_reaches_remainder_and_then_zero')
        self.case.BACKEND = self.BACKEND
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.conn = self.case.conn

    def crawler(self, cursor=None):
        c = self.case.crawler()
        c.delivery_mode = 'backfill'
        c.delivery_cursor = cursor
        return c

    def test_partial_single_page_resume_keeps_linked_cards_and_avoids_old_details(self):
        cursor = None
        for before, expected in ((0, 3), (3, 3), (6, 2), (8, 0)):
            c = self.crawler(cursor)
            self.assertEqual(c.crawl(limit=3), expected)
            cursor = c.delivery_pending_cursor
            self.assertEqual((cursor['page'], cursor['offset']), (1, before + expected))
            fetched = [call.args[0] for call in self.case.fetch.call_args_list]
            self.assertEqual(fetched, [self.case.module._LIST_URL] +
                             [self.case.urls[n] for n in range(before + 1, before + expected + 1) if n > 2])
        self.assertTrue(c.delivery_exhausted)
        self.assertEqual(self.case.count(), 8)

    def test_cancel_and_reopen_preserve_committed_position(self):
        c = self.crawler();c.delivery_should_cancel = lambda: self.case.count() >= 3
        with self.assertRaises(CrawlCancelled):c.crawl()
        self.assertEqual(c.delivery_pending_cursor['offset'], 3)
        c = self.crawler(c.delivery_pending_cursor)
        self.assertEqual(c.crawl(), 5);self.assertTrue(c.delivery_exhausted)

    def test_interrupted_after_commit_recovers_overlap_without_duplicate(self):
        c = self.crawler()
        def committed(seq):
            if self.case.count() == 3:raise InterruptedAfterCommit()
        c.delivery_document_saved = committed
        with self.assertRaises(InterruptedAfterCommit):c.crawl()
        self.assertEqual(c.delivery_pending_cursor['offset'], 2)
        self.assertEqual(self.crawler(c.delivery_pending_cursor).crawl(), 5)
        self.assertEqual(self.case.count(), 8)

    def test_source_reordering_or_title_change_stops_before_new_details(self):
        c = self.crawler();c.crawl(limit=3);cursor = c.delivery_pending_cursor
        c = self.crawler(cursor);fetch = self.case.fetch.side_effect
        self.case.fetch.side_effect = lambda url, **kw: fetch(url, **kw).replace('Report 1', 'Changed report')
        self.assertEqual(c.crawl(), 0);self.assertEqual(c.delivery_source_failure, 'invalid_response')
        self.assertIsNone(c.delivery_pending_cursor);self.assertEqual(self.case.fetch.call_count, 1)

    def test_stored_body_change_stops_resume(self):
        c = self.crawler();c.crawl(limit=3);cursor = c.delivery_pending_cursor
        self.conn.execute("UPDATE documents SET abstract='Changed stored body' WHERE seq_id=1");self.conn.commit()
        c = self.crawler(cursor);self.assertEqual(c.crawl(), 0)
        self.assertEqual(c.delivery_source_failure, 'invalid_response')

    def test_missing_prefix_row_stops_resume(self):
        c = self.crawler();c.crawl(limit=3);cursor = c.delivery_pending_cursor
        self.conn.execute('DELETE FROM documents WHERE seq_id=1');self.conn.commit()
        c = self.crawler(cursor);self.assertEqual(c.crawl(), 0);self.assertFalse(c.delivery_exhausted)

    def test_file_flags_do_not_change_source_anchor(self):
        c = self.crawler();c.crawl(limit=3);cursor = c.delivery_pending_cursor
        self.conn.execute('UPDATE documents SET pdf_downloaded=1,text_extracted=1 WHERE seq_id=1');self.conn.commit()
        self.assertEqual(self.crawler(cursor).crawl(), 5)

    def test_existing_key_with_changed_URL_is_held_without_new_row(self):
        c = self.crawler();c.crawl(limit=3)
        self.conn.execute("UPDATE documents SET meta_url='https://changed.invalid/source' WHERE seq_id=1");self.conn.commit()
        c = self.crawler();self.assertEqual(c.crawl(), 0);self.assertEqual(c.delivery_source_failure, 'item_failed')
        self.assertEqual(self.case.count(), 3)

    def test_body_gap_stays_at_unaccepted_source_item(self):
        c = self.crawler();fetch = self.case.fetch.side_effect
        self.case.fetch.side_effect = lambda url, **kw: '<h1 class="entry-title">Empty report</h1>' if url.endswith('/report-4/') else fetch(url, **kw)
        # Clear the listing fallback only for this real source-shape fixture.
        native = c._parse_list
        def listing(soup):
            records = native(soup)
            for record in records:
                if record['post_id'] == '4':record['excerpt'] = ''
            return records
        c._parse_list = listing
        self.assertEqual(c.crawl(), 3);self.assertEqual(c.delivery_source_failure, 'item_failed')
        self.assertEqual(c.delivery_pending_cursor['offset'], 3);self.assertFalse(c.delivery_exhausted)

    def test_empty_or_skipped_invalid_card_does_not_complete_catalogue(self):
        for html in ('<main>Missing catalogue</main>', '<article class="publications post-1"><a href="javascript:bad"><h4>Bad</h4></a></article>'):
            c = self.crawler();self.case.fetch.side_effect = None;self.case.fetch.return_value = html
            self.assertEqual(c.crawl(), 0);self.assertEqual(c.delivery_source_failure, 'invalid_response');self.assertFalse(c.delivery_exhausted)

    def test_invalid_cursor_rejected_without_requests(self):
        c = self.crawler({'version':1,'page':False})
        self.assertEqual(c.crawl(), 0);self.case.fetch.assert_not_called()

    def test_previous_pdf_role_contract_requires_explicit_cursor_review(self):
        from crawler.canurb_traversal import digest
        c = self.crawler();self.assertEqual(c.crawl(limit=3),3)
        cursor = dict(c.delivery_pending_cursor)
        cursor['contract'] = digest(['canurb-publications-v1',self.case.module._LIST_URL,
                                     'publisher_cards_including_linked_PDF_and_external_publications'])
        c = self.crawler(cursor)
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'configuration')
        self.case.fetch.assert_not_called()

    def test_next_page_must_be_the_adjacent_publisher_catalogue(self):
        c = self.crawler();fetch = self.case.fetch.side_effect
        self.case.fetch.side_effect = lambda url, **kw: fetch(url, **kw) + '<a rel="next" href="https://other.invalid/page/2/">Next</a>'
        self.assertEqual(c.crawl(), 0);self.assertEqual(c.delivery_source_failure, 'invalid_response')

    def test_next_page_and_cap_and_earlier_page_drift(self):
        def configure(cursor=None, drift=False):
            c = self.crawler(cursor);fetch = self.case.fetch.side_effect
            listing = fetch(self.case.module._LIST_URL);cards = [str(n) for n in c._make_soup(listing).select('article')]
            def responses(url, **kwargs):
                if url == self.case.module._LIST_URL:
                    raw = '<main>'+''.join(cards[:5])+'</main><a rel="next" href="/publications/page/2/">Next</a>'
                    return raw.replace('Report 1', 'Changed report') if drift else raw
                if url == self.case.module._LIST_URL+'page/2/':return '<main>'+''.join(cards[5:])+'</main>'
                return fetch(url, **kwargs)
            self.case.fetch.side_effect = responses;return c
        c = configure()
        with patch.object(self.case.module, '_PAGE_SAFETY_CAP', 1):self.assertEqual(c.crawl(), 5)
        self.assertEqual(c.delivery_source_failure, 'invalid_response');self.assertFalse(c.delivery_exhausted)
        cursor = c.delivery_pending_cursor;c = configure(cursor)
        self.assertEqual(c.crawl(limit=1), 1);self.assertEqual((c.delivery_pending_cursor['page'],c.delivery_pending_cursor['offset']),(2,1))
        cursor = c.delivery_pending_cursor;c = configure(cursor, drift=True)
        self.assertEqual(c.crawl(), 0);self.assertEqual(c.delivery_source_failure, 'invalid_response')
        c = configure(cursor);self.assertEqual(c.crawl(), 2);self.assertTrue(c.delivery_exhausted)

    def test_incremental_preserves_backfill_cursor(self):
        c = self.crawler();c.crawl(limit=3);cursor = c.delivery_pending_cursor
        c = self.crawler(cursor);c.delivery_mode = 'incremental'
        self.assertEqual(c.crawl(), 5);self.assertIsNone(c.delivery_pending_cursor)

    def test_status_partial_response_and_certificate_verification(self):
        for status, code, expected in ((403,0,'authentication_failed'),(429,0,'rate_limited'),(503,0,'rate_limited'),(200,28,None),(500,0,None)):
            c = self.crawler();result = SimpleNamespace(stdout=b'<main>Error</main>\n'+str(status).encode(),stderr=b'',returncode=code)
            with patch.object(self.case.module.subprocess, 'run', return_value=result) as run:
                self.assertIsNone(self.case.module.CanurbOrgPublicationsCrawler._curl_get(c,c.base_url))
            self.assertEqual(c.delivery_source_failure,expected)
            self.assertNotIn('-skL',run.call_args.args[0]);self.assertNotIn('--insecure',run.call_args.args[0])
            self.assertEqual(run.call_count,1 if status < 500 or status == 503 else 3)


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')
class CanurbCursorPostgresTests(CanurbCursorTests):
    BACKEND = 'postgres'

    def test_worker_persists_cancelled_position_and_completion(self):
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        init_delivery_schema(self.conn)
        for index,(limit,cancel,wanted) in enumerate(((3,None,3),(3,2,2),(3,None,3),(None,None,0))):
            c = self.crawler();before = self.case.count()
            if index in (0,2,3):jobs.enqueue_job(self.conn,self.case.SITE,mode='backfill',limit_n=limit)
            job = jobs.claim_next_job(self.conn);self.assertIsNotNone(job)
            def cancelled():
                if cancel and self.case.count()>=before+cancel:jobs.cancel_job(self.conn,job['id']);return True
                return False
            self.assertEqual(worker.run_job(self.conn,job,{self.case.SITE:lambda **kw:c},delay=0,should_cancel=cancelled),wanted)
            state = jobs.job_detail(self.conn,job['id'])['job'];self.assertEqual(state['saved_count'],wanted);self.assertEqual(state['status'],'cancelled' if cancel else 'done')
        progress = self.conn.execute('SELECT * FROM crawl_site_progress').fetchone()
        self.assertEqual(progress['items_done'],8);self.assertIsNotNone(progress['completed_at']);self.assertEqual(progress['cursor']['offset'],8)
