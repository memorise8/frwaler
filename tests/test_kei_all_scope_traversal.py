"""Real all-scope parsing, saves, source drift and persisted-prefix resumes."""
import json
import os
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from crawler.base_crawler import CrawlCancelled
from crawler.kei_source import SCOPES
from tests.test_delivery_site_new_counts import SiteContract
from tests.test_kei_details import fixture as detail_fixture
from tests.test_kei_source_bounds import fixture as list_fixture


class KeiTraversal(unittest.TestCase):
    SITE = 'kei-re-kr-eliblistes'
    BACKEND = 'sqlite'
    setUp = SiteContract.setUp
    count = SiteContract.count

    def crawler(self):
        c = self.module.KeiReKrEliblistesCrawler(self.conn, delay=0)
        c._RATE_SLEEP = 0
        # Traversal fixtures have no retained file bytes. PDF transport and
        # extraction outcomes are exercised separately in test_kei_pdf_body.
        c._session.get = Mock(side_effect=ValueError('fixture PDF text unavailable'))
        self.addCleanup(c._session.close)
        self.calls = []

        def get(url, **kwargs):
            q = parse_qs(urlsplit(url).query)
            scope = 'board-' + q['bid'][0] if 'bid' in q else q['elibName'][0]
            self.calls.append((scope, q.get('act', ['list'])[0], url))
            if q.get('act') != ['view']:
                page = int(q.get('nPage', ['1'])[0])
                raw = list_fixture(scope, page, 12 if scope == 'researchreport' else 2)
            else:
                uid = q.get('c_id', q.get('list_no'))[0]
                raw, item = detail_fixture(scope, body='<p>' + 'Actual source body. ' * 12 + '</p>',
                                           filename='press.hwp' if scope == 'board-0009' else 'report.pdf')
                raw = raw.replace('123', uid).replace(item['title'], 'Environmental research ' + uid)
            return getattr(self, 'change_source', lambda raw, scope, q: raw)(raw, scope, q)

        c._curl_get = get
        return c

    def rows(self):
        cur = self.conn.execute('SELECT * FROM documents ORDER BY seq_id')
        names = [r[0] for r in cur.description]
        return [dict(r) if hasattr(r, 'keys') else dict(zip(names, r)) for r in cur]

    def test_malformed_body_identity_stops_before_save_or_PDF_fallback(self):
        def change(raw, scope, query):
            if query.get('act') == ['view']:
                return raw.replace('<div id="view2">', '<span id="view2">')
            return raw
        self.change_source = change
        c = self.crawler(); c.delivery_mode = 'backfill'
        self.assertEqual(c.crawl(limit=2), 0)
        self.assertEqual(self.count(), 0)
        self.assertEqual(c.delivery_source_failure, 'item_failed')
        self.assertFalse(c.delivery_exhausted)
        self.assertEqual(c.delivery_pending_cursor['offset'], 0)
        c._session.get.assert_not_called()

    def test_known_damaged_body_uses_only_validated_PDF_text_and_preserves_identity(self):
        def change(raw, scope, query):
            if query.get('act') == ['view']:
                return raw.replace('<div id="view1">목차: 방법, 결과</div>',
                                   '<div id="view1"><p><span style="letter-spacing: -0.\n</div>').replace(
                                       '<div id="view2">', '<div class="view" id="view2">')
            return raw
        self.change_source = change
        c = self.crawler(); c.delivery_mode = 'backfill'
        excerpt = 'Actual verified source PDF report text. ' * 10
        with patch('crawler.kei_pdf.source_pdf_excerpt', return_value=(excerpt, {})) as recover:
            self.assertEqual(c.crawl(limit=1), 1)
        detail = recover.call_args.args[2]
        self.assertIsNone(detail['abstract'])
        self.assertEqual(detail['body_classification'], 'malformed_source_HTML_requires_PDF')
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['abstract'], excerpt)
        self.assertEqual(rows[0]['post_number'], detail['source_key'])
        self.assertEqual(rows[0]['pdf_url'], detail['primary_PDF_candidate']['url'])
        self.assertFalse(rows[0]['pdf_downloaded'])
        before = self.rows()
        c = self.crawler(); c.delivery_mode = 'backfill'
        with patch('crawler.kei_pdf.source_pdf_excerpt', side_effect=ValueError('pdf_copy_restricted')):
            self.assertEqual(c.crawl(limit=1), 0)
        self.assertEqual(c.delivery_source_failure, 'item_failed')
        self.assertEqual(self.rows(), before)
        self.assertEqual(c.delivery_pending_cursor['offset'], 0)

    def test_all_eleven_scopes_same_numeric_ids_repeat_zero_and_date_roles(self):
        c = self.crawler()
        self.assertEqual(c.crawl(), 32)
        self.assertTrue(c.delivery_exhausted)
        self.assertIsNone(c.delivery_source_failure)
        rows = self.rows()
        self.assertEqual(len({r['post_number'] for r in rows}), 32)
        self.assertEqual({r['post_number'].split(':')[0] for r in rows}, set(SCOPES))
        for r in rows:
            self.assertFalse(r['pdf_downloaded'])
            self.assertFalse(r['text_extracted'])
            if r['post_number'].startswith('board-0009:'):
                self.assertIsNone(r['published_date'])
                self.assertEqual(r['listed_date_raw'], '2026-09-30 14:41')
                self.assertFalse(r['pdf_url'])
        c = self.crawler()
        self.assertEqual(c.crawl(), 0)
        self.assertTrue(c.delivery_exhausted)
        self.assertEqual(self.rows(), rows)

    def test_mid_page_then_cross_scope_resume_preserves_prefix_rows(self):
        c = self.crawler(); c.delivery_mode = 'backfill'
        self.assertEqual(c.crawl(limit=11), 11)
        cursor = c.delivery_pending_cursor
        self.assertEqual((cursor['scope'], cursor['page'], cursor['offset']), ('researchreport', 2, 1))
        before = self.rows()
        c = self.crawler(); c.delivery_mode = 'backfill'; c.delivery_cursor = cursor
        self.assertEqual(c.crawl(limit=3), 3)
        self.assertEqual(self.rows()[:11], before)
        self.assertEqual([r[0] for r in self.calls if r[1] == 'view'],
                         ['researchreport', 'environmentalforum', 'environmentalforum'])
        cursor = c.delivery_pending_cursor
        self.assertEqual((cursor['scope'], cursor['offset']), ('environmentalforum', 2))
        c = self.crawler(); c.delivery_mode = 'backfill'; c.delivery_cursor = cursor
        self.assertEqual(c.crawl(), 18)
        self.assertTrue(c.delivery_exhausted)
        self.assertEqual(self.count(), 32)

    def test_cancel_after_commit_replays_overlap_without_new_duplicate(self):
        c = self.crawler(); c.delivery_mode = 'backfill'; cancel = []
        c.delivery_document_saved = lambda seq: cancel.append(seq)
        c.delivery_should_cancel = lambda: len(cancel) == 3
        with self.assertRaises(CrawlCancelled): c.crawl()
        self.assertEqual(self.count(), 3)
        cursor = c.delivery_pending_cursor
        c = self.crawler(); c.delivery_mode = 'backfill'; c.delivery_cursor = cursor
        self.assertEqual(c.crawl(), 29)
        self.assertEqual(self.count(), 32)
        self.assertTrue(c.delivery_exhausted)

    def test_earlier_page_change_holds_before_any_new_details(self):
        c = self.crawler(); c.delivery_mode = 'backfill'; c.crawl(limit=11)
        cursor = c.delivery_pending_cursor; before = self.rows()
        self.change_source = lambda raw, scope, q: raw.replace('2026-08-31', '2026-08-30') if scope == 'researchreport' and q.get('nPage', ['1']) == ['1'] and 'act' not in q else raw
        c = self.crawler(); c.delivery_mode = 'backfill'; c.delivery_cursor = cursor
        self.assertEqual(c.crawl(), 0)
        self.assertEqual(c.delivery_source_failure, 'invalid_response')
        self.assertEqual(self.rows(), before)
        self.assertFalse(any(r[1] == 'view' for r in self.calls))

    def test_stored_prefix_mutation_or_missing_row_holds(self):
        c = self.crawler(); c.delivery_mode = 'backfill'; c.crawl(limit=11)
        cursor = c.delivery_pending_cursor
        self.conn.execute("UPDATE documents SET title='Changed source field'"); self.conn.commit()
        c = self.crawler(); c.delivery_mode = 'backfill'; c.delivery_cursor = cursor
        self.assertEqual(c.crawl(), 0)
        self.assertEqual(c.delivery_source_failure, 'invalid_response')
        self.assertFalse(any(r[1] == 'view' for r in self.calls))

    def test_file_flags_do_not_invalidate_stored_source_prefix(self):
        c = self.crawler(); c.delivery_mode = 'backfill'; c.crawl(limit=11)
        cursor = c.delivery_pending_cursor
        self.conn.execute('UPDATE documents SET pdf_downloaded=1,text_extracted=1'); self.conn.commit()
        c = self.crawler(); c.delivery_mode = 'backfill'; c.delivery_cursor = cursor
        self.assertEqual(c.crawl(limit=1), 1)
        self.assertIsNone(c.delivery_source_failure)
        self.assertTrue(all(r['pdf_downloaded'] for r in self.rows()[:11]))

    def test_historical_research_alias_is_preserved_and_anchored(self):
        c = self.crawler(); c.crawl(limit=1)
        self.conn.execute("UPDATE documents SET post_number='legacy-import:one'"); self.conn.commit()
        before = self.rows()[0]
        c = self.crawler(); c.delivery_mode = 'backfill'
        self.assertEqual(c.crawl(limit=1), 1)
        self.assertEqual(c.delivery_cursor_items_done, 2)
        cursor = c.delivery_pending_cursor
        c = self.crawler(); c.delivery_mode = 'backfill'; c.delivery_cursor = cursor
        self.assertEqual(c.crawl(limit=1), 1)
        self.assertEqual(self.rows()[0], before)

    def test_body_gap_holds_cursor_without_skipping_source_id(self):
        self.change_source = lambda raw, scope, q: raw.replace('Actual source body. ' * 12, '') if scope == 'environmentalforum' else raw
        c = self.crawler(); c.delivery_mode = 'backfill'
        self.assertEqual(c.crawl(), 12)
        cursor = c.delivery_pending_cursor
        self.assertEqual((cursor['scope'], cursor['page'], cursor['offset']), ('environmentalforum', 1, 0))
        self.assertEqual(c.delivery_source_failure, 'item_failed')
        c = self.crawler(); c.delivery_mode = 'backfill'; c.delivery_cursor = cursor
        self.assertEqual(c.crawl(), 0)
        self.assertEqual(c.delivery_pending_cursor, cursor)
        self.assertFalse(c.delivery_exhausted)

    def test_incremental_keeps_other_scopes_but_reports_body_gaps(self):
        self.change_source = lambda raw, scope, q: raw.replace('Actual source body. ' * 12, '') if scope == 'board-0028' else raw
        c = self.crawler()
        self.assertEqual(c.crawl(), 30)
        self.assertEqual(c.delivery_source_failure, 'item_failed')
        self.assertFalse(c.delivery_exhausted)
        self.assertIsNone(c.delivery_pending_cursor)

    def test_corrupt_title_stays_in_denominator_and_is_not_saved(self):
        self.change_source = lambda raw, scope, q: raw.replace('Environmental research 1', 'Damaged\x00title') if scope == 'board-0009' else raw
        c = self.crawler()
        self.assertEqual(c.crawl(), 31)
        self.assertEqual(c.delivery_source_failure, 'item_failed')
        self.assertFalse(c.delivery_exhausted)

    def test_transport_failure_never_skips_to_a_later_scope(self):
        c = self.crawler(); original = c._curl_get
        c._curl_get = lambda url, **kw: None if 'elibName=environmentalforum' in url else original(url, **kw)
        self.assertEqual(c.crawl(), 12)
        self.assertEqual(c.delivery_source_failure, 'fetch_failed')
        self.assertFalse(any(r[0].startswith('board-') for r in self.calls))

    def test_malformed_cursor_rejected_before_requests(self):
        c = self.crawler(); c.delivery_mode = 'backfill'; c.delivery_cursor = {'version': 1}
        self.assertEqual(c.crawl(), 0)
        self.assertEqual(c.delivery_source_failure, 'configuration')
        self.assertEqual(self.calls, [])

    def test_page_budget_and_zero_limit_do_not_claim_exhaustion(self):
        c = self.crawler()
        self.assertEqual(c.crawl(limit=0), 0)
        self.assertEqual(self.calls, [])
        c._MAX_PAGES = 1
        self.assertEqual(c.crawl(), 10)
        self.assertEqual(c.delivery_source_failure, 'invalid_response')
        self.assertFalse(c.delivery_exhausted)


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')
class KeiTraversalPostgres(KeiTraversal):
    BACKEND = 'postgres'

    def test_worker_persists_actual_partial_cursor_and_completes_next_job(self):
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs, worker
        init_delivery_schema(self.conn)
        case = self
        class Factory:
            DELIVERY_CURSOR_VERSION = 1
            def __new__(cls, **kwargs): return case.crawler()
        jobs.enqueue_job(self.conn, self.SITE, mode='backfill', limit_n=11)
        job = jobs.claim_next_job(self.conn)
        self.assertEqual(worker.run_job(self.conn, job, {self.SITE: Factory}, delay=0), 11)
        self.assertEqual(jobs.load_cursor(self.conn, self.SITE)['offset'], 1)
        job = jobs.claim_next_job(self.conn)
        if job is None:
            jobs.enqueue_job(self.conn, self.SITE, mode='backfill')
            job = jobs.claim_next_job(self.conn)
        # Auto continuation retains the requested per-job budget.
        while job is not None:
            worker.run_job(self.conn, job, {self.SITE: Factory}, delay=0)
            job = jobs.claim_next_job(self.conn)
        self.assertEqual(self.count(), 32)
        progress = self.conn.execute('SELECT * FROM crawl_site_progress WHERE site_id=%s', (self.SITE,)).fetchone()
        self.assertIsNotNone(progress['completed_at'])
        self.assertEqual(progress['items_done'], 32)


if __name__ == '__main__': unittest.main()
