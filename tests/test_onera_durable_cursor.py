"""ONERA source/DB drift and native database cursor lifecycle contracts."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from crawler.base_crawler import CrawlCancelled
from tests import test_publication_press_contracts as fixtures
from tests.test_delivery_site_new_counts import InterruptedAfterCommit


class OneraCursorTests(unittest.TestCase):
    BACKEND = 'sqlite'

    def setUp(self):
        self.case = fixtures.OneraCounts(methodName='test_zero_limit_no_fetch')
        self.case.BACKEND = self.BACKEND
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.conn = self.case.conn

    def crawler(self, cursor=None):
        c = self.case.crawler()
        c.delivery_mode = 'backfill'
        c.delivery_cursor = cursor
        return c

    def test_reopened_crawler_resumes_partial_pages_without_detail_replays(self):
        cursor = None
        for expected, position in ((3, (0, 3)), (3, (1, 1)), (2, (1, 3)), (0, (1, 3))):
            c = self.crawler(cursor)
            self.assertEqual(c.crawl(limit=3), expected)
            cursor = c.delivery_pending_cursor
            self.assertEqual((cursor['page'], cursor['offset']), position)
            details = [v.args[0] for v in self.case.fetch.call_args_list if '?page=' not in v.args[0]]
            self.assertEqual(len(details), expected)
        self.assertTrue(c.delivery_exhausted)
        self.assertEqual(self.case.count(), 8)

    def test_cancel_preserves_exact_committed_offset(self):
        c = self.crawler();c.delivery_should_cancel = lambda: self.case.count() >= 2
        with self.assertRaises(CrawlCancelled):c.crawl()
        self.assertEqual(c.delivery_pending_cursor['offset'], 2)
        c = self.crawler(c.delivery_pending_cursor)
        self.assertEqual(c.crawl(), 6);self.assertTrue(c.delivery_exhausted)

    def test_post_commit_interruption_recovers_one_overlap_without_duplicate(self):
        c = self.crawler()
        def committed(seq):
            if self.case.count() == 2:raise InterruptedAfterCommit()
        c.delivery_document_saved = committed
        with self.assertRaises(InterruptedAfterCommit):c.crawl()
        self.assertEqual(c.delivery_pending_cursor['offset'], 1)
        self.assertEqual(self.crawler(c.delivery_pending_cursor).crawl(), 6)
        self.assertEqual(self.case.count(), 8)

    def test_earlier_page_drift_stops_before_new_detail_requests(self):
        c = self.crawler();self.assertEqual(c.crawl(limit=6), 6)
        c = self.crawler(c.delivery_pending_cursor);original = self.case.fetch.side_effect
        self.case.fetch.side_effect = lambda url, **kw: original(url, **kw).replace('Report 1', 'Changed first source title') if '?page=0' in url else original(url, **kw)
        self.assertEqual(c.crawl(), 0);self.assertEqual(c.delivery_source_failure, 'invalid_response')
        self.assertIsNone(c.delivery_pending_cursor)
        self.assertFalse(any('?page=' not in v.args[0] for v in self.case.fetch.call_args_list))

    def test_stored_body_drift_stops_resume(self):
        c = self.crawler();c.crawl(limit=3);cursor = c.delivery_pending_cursor
        self.conn.execute("UPDATE documents SET abstract='Changed stored source body' WHERE seq_id=1");self.conn.commit()
        c = self.crawler(cursor);self.assertEqual(c.crawl(), 0)
        self.assertEqual(c.delivery_source_failure, 'invalid_response');self.assertFalse(c.delivery_exhausted)

    def test_missing_stored_prefix_stops_resume(self):
        c = self.crawler();c.crawl(limit=3);cursor = c.delivery_pending_cursor
        self.conn.execute('DELETE FROM documents WHERE seq_id=1');self.conn.commit()
        c = self.crawler(cursor);self.assertEqual(c.crawl(), 0);self.assertEqual(c.delivery_source_failure, 'invalid_response')

    def test_file_flags_do_not_invalidate_source_anchor(self):
        c = self.crawler();c.crawl(limit=3);cursor = c.delivery_pending_cursor
        self.conn.execute('UPDATE documents SET pdf_downloaded=1,text_extracted=1 WHERE seq_id=1');self.conn.commit()
        self.assertEqual(self.crawler(cursor).crawl(), 5)

    def test_body_gap_holds_cursor_at_failed_document(self):
        self.case.excluded = {4};c = self.crawler()
        self.assertEqual(c.crawl(), 3);self.assertEqual(c.delivery_pending_cursor['offset'], 3)
        self.assertEqual(c.delivery_source_failure, 'item_failed');self.assertFalse(c.delivery_exhausted)
        self.case.excluded = set();self.assertEqual(self.crawler(c.delivery_pending_cursor).crawl(), 5)

    def test_invalid_cursor_rejected_without_requests(self):
        c = self.crawler({'version': 1, 'page': False})
        self.assertEqual(c.crawl(), 0);self.assertEqual(c.delivery_source_failure, 'configuration');self.case.fetch.assert_not_called()

    def test_page_cap_is_incomplete_with_saved_prefix(self):
        c = self.crawler()
        with patch.object(self.case.module, '_MAX_PAGES', 1):self.assertEqual(c.crawl(), 5)
        self.assertEqual(c.delivery_source_failure, 'invalid_response');self.assertFalse(c.delivery_exhausted)
        self.assertEqual(self.crawler(c.delivery_pending_cursor).crawl(), 3)

    def test_empty_listing_cannot_complete_scope(self):
        c = self.crawler();self.case.fetch.return_value = '<main>No listing structure</main>';self.case.fetch.side_effect = None
        self.assertEqual(c.crawl(), 0);self.assertEqual(c.delivery_source_failure, 'invalid_response');self.assertFalse(c.delivery_exhausted)

    def test_cross_host_pagination_held_before_saves(self):
        c = self.crawler();original = self.case.fetch.side_effect
        self.case.fetch.side_effect = lambda url, **kw: original(url, **kw).replace('href="?page=1"', 'href="https://outside.invalid/?page=1"')
        self.assertEqual(c.crawl(), 0);self.assertEqual(c.delivery_source_failure, 'invalid_response')

    def test_incremental_run_does_not_publish_backfill_cursor(self):
        c = self.crawler();c.crawl(limit=3);cursor = c.delivery_pending_cursor
        c = self.crawler(cursor);c.delivery_mode = 'incremental'
        self.assertEqual(c.crawl(), 5);self.assertIsNone(c.delivery_pending_cursor);self.assertEqual(c.delivery_cursor, cursor)

    def test_transport_status_and_partial_body_not_accepted_as_HTML(self):
        for status, returncode, expected in [(403, 0, 'authentication_failed'), (429, 0, 'rate_limited'), (503, 0, 'rate_limited'), (200, 28, None), (500, 0, None)]:
            with self.subTest(status=status, returncode=returncode):
                c = self.crawler();response = SimpleNamespace(stdout=b'<main>Server response</main>\n'+str(status).encode(), stderr=b'', returncode=returncode)
                with patch.object(self.case.module.subprocess, 'run', return_value=response) as run:
                    self.assertIsNone(self.case.module.OneraFrFrCrawler._fetch(c, c.base_url))
                self.assertEqual(c.delivery_source_failure, expected)
                self.assertEqual(run.call_count, 1 if status < 500 or status == 503 else 3)


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')
class OneraCursorPostgresTests(OneraCursorTests):
    BACKEND = 'postgres'

    def test_real_worker_continuation_and_cancel_cursor(self):
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs, worker
        init_delivery_schema(self.conn)
        for index, (limit, cancel, expected) in enumerate([(3, None, 3), (3, 2, 2), (3, None, 3), (None, None, 0)]):
            c = self.crawler();before = self.case.count()
            if index in (0, 2, 3):jobs.enqueue_job(self.conn, self.case.SITE, mode='backfill', limit_n=limit)
            job = jobs.claim_next_job(self.conn);self.assertIsNotNone(job)
            def should_cancel():
                if cancel and self.case.count() >= before + cancel:
                    jobs.cancel_job(self.conn, job['id']);return True
                return False
            result = worker.run_job(self.conn, job, {self.case.SITE: lambda **kw: c}, delay=0, should_cancel=should_cancel)
            self.assertEqual(result, expected)
            detail = jobs.job_detail(self.conn, job['id'])['job'];self.assertEqual(detail['saved_count'], expected)
            self.assertEqual(detail['status'], 'cancelled' if cancel else 'done')
        progress = self.conn.execute('SELECT * FROM crawl_site_progress').fetchone()
        self.assertEqual(progress['items_done'], 8);self.assertIsNotNone(progress['completed_at'])
        self.assertEqual((progress['cursor']['page'], progress['cursor']['offset']), (1, 3))
