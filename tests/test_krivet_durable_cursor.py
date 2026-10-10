"""KRIVET backfill checkpoints preserve row positions, including source duplicates."""
import json
import os
import unittest
from unittest.mock import patch

from crawler.base_crawler import CrawlCancelled
from tests.test_krivet_delivery_scopes import KrivetScopes, detail, listing


class KrivetCursor(KrivetScopes):
    def backfill(self, cursor=None):
        c = self.crawler()
        c.delivery_mode = 'backfill'
        c.delivery_cursor = cursor
        return c

    def test_partial_page_survives_new_crawler_across_all_four_menus(self):
        cursor = None
        for expected in (3, 3, 2):
            c = self.backfill(cursor)
            self.assertEqual(c.crawl(limit=3), expected)
            self.assertIsNotNone(c.delivery_pending_cursor)
            cursor = json.loads(json.dumps(c.delivery_pending_cursor))
        self.assertEqual(self.count(), 8)
        self.assertTrue(c.delivery_exhausted)
        c = self.backfill(cursor)
        self.assertEqual(c.crawl(), 0)
        self.assertTrue(c.delivery_exhausted)

    def test_cancel_preserves_committed_offset_and_does_not_rescan_prior_menu(self):
        c = self.backfill()
        c.delivery_should_cancel = lambda: self.count() >= 3
        with self.assertRaises(CrawlCancelled):
            c.crawl()
        cursor = c.delivery_pending_cursor
        self.assertEqual((cursor['menu'], cursor['page'], cursor['offset']), ('19', 1, 1))
        c = self.backfill(cursor)
        self.assertEqual(c.crawl(), 5)
        menus = [x.args[1]['menuSn'] for x in self.fetch.call_args_list]
        self.assertNotIn('15', menus)

    def test_save_failure_holds_position_until_recovered(self):
        c = self.backfill()
        native = c._save_paper
        def save(paper):
            if paper['external_id'] == 'PB4':
                raise RuntimeError('controlled persistence failure')
            return native(paper)
        c._save_paper = save
        self.assertEqual(c.crawl(), 3)
        self.assertEqual(c.delivery_source_failure, 'item_failed')
        cursor = c.delivery_pending_cursor
        self.assertEqual((cursor['menu'], cursor['offset']), ('19', 1))
        c = self.backfill(cursor)
        self.assertEqual(c.crawl(), 5)
        self.assertTrue(c.delivery_exhausted)

    def test_body_gap_is_not_skipped_or_completed(self):
        c = self.backfill()
        original = self.fetch.side_effect
        self.fetch.side_effect = lambda u,p,**kw: detail('15','PB2',body='') if p.get('pstNo') == 'PB2' else original(u,p,**kw)
        self.assertEqual(c.crawl(), 1)
        self.assertEqual(c.delivery_pending_cursor['offset'], 1)
        self.assertEqual(c.delivery_source_failure, 'item_failed')
        self.assertFalse(c.delivery_exhausted)

    def test_current_page_identity_drift_stops_before_saving(self):
        c = self.backfill(); c.crawl(limit=3)
        c = self.backfill(c.delivery_pending_cursor)
        original = self.fetch.side_effect
        self.fetch.side_effect = lambda u,p,**kw: original(u,p,**kw).replace('PB3', 'PB99') if 'pageIndex' in p else original(u,p,**kw)
        self.assertEqual(c.crawl(), 0)
        self.assertEqual(c.delivery_source_failure, 'invalid_response')
        self.assertIsNone(c.delivery_pending_cursor)

    def test_now_empty_saved_page_is_not_false_completion(self):
        c = self.backfill(); c.crawl(limit=3)
        c = self.backfill(c.delivery_pending_cursor)
        self.fetch.side_effect = lambda u,p,**kw: listing(p['menuSn'], [])
        self.assertEqual(c.crawl(), 0)
        self.assertEqual(c.delivery_source_failure, 'invalid_response')
        self.assertFalse(c.delivery_exhausted)
        self.assertIsNone(c.delivery_pending_cursor)

    def test_explicit_empty_menu_needs_no_pagination_widget(self):
        c = self.backfill()
        self.fetch.side_effect = lambda u,p,**kw: listing(p['menuSn'], []).split('<a class="page_last"')[0]
        self.assertEqual(c.crawl(), 0)
        self.assertFalse(c.delivery_source_failure)
        self.assertTrue(c.delivery_exhausted)

    def test_missing_current_page_or_changed_boundary_rejects_saved_offset(self):
        c = self.backfill(); c.crawl(limit=3); cursor = c.delivery_pending_cursor
        for old,new in [('title="선택됨"','title="unknown"'),('selectList(1)','selectList(2)')]:
            c = self.backfill(cursor); original = self.fetch.side_effect
            self.fetch.side_effect = lambda u,p,**kw: original(u,p,**kw).replace(old,new)
            self.assertEqual(c.crawl(), 0)
            self.assertEqual(c.delivery_source_failure, 'invalid_response')
            self.assertIsNone(c.delivery_pending_cursor)

    def paged(self, cursor=None):
        c = self.backfill(cursor)
        def fetch(u,p,**kw):
            if 'pstNo' in p:
                return detail(p['menuSn'],p['pstNo'])
            if p['menuSn'] != '15':
                return listing(p['menuSn'], [])
            page = int(p['pageIndex'])
            return listing('15', ['PB1','PB2'] if page==1 else ['PB2','PB3'], page, 2)
        self.fetch.side_effect = fetch
        return c

    def test_duplicate_row_checkpoint_and_previous_page_anchor(self):
        c = self.paged(); self.assertEqual(c.crawl(limit=2), 2)
        c = self.paged(c.delivery_pending_cursor)
        self.assertEqual(c.crawl(), 1)
        self.assertEqual(self.count(), 3)
        self.assertTrue(c.delivery_exhausted)
        self.assertEqual(c.delivery_cursor_items_done, 2)

    def test_previous_page_drift_is_detected_on_later_page_resume(self):
        c = self.paged(); self.assertEqual(c.crawl(limit=3), 3)
        cursor = c.delivery_pending_cursor
        self.assertEqual(cursor['page'], 2)
        c = self.paged(cursor); original = self.fetch.side_effect
        self.fetch.side_effect = lambda u,p,**kw: original(u,p,**kw).replace('PB1','PB99') if p.get('pageIndex')=='1' else original(u,p,**kw)
        self.assertEqual(c.crawl(), 0)
        self.assertEqual(c.delivery_source_failure, 'invalid_response')
        self.assertIsNone(c.delivery_pending_cursor)

    def test_wall_budget_preserves_position_for_next_job(self):
        c = self.backfill()
        with patch.object(self.module, '_WALL_CLOCK_LIMIT', .5), patch.object(self.module.time,'monotonic',side_effect=lambda:1. if self.count()>=3 else 0.):
            self.assertEqual(c.crawl(), 3)
        self.assertFalse(c.delivery_source_failure)
        self.assertFalse(c.delivery_exhausted)
        c = self.backfill(c.delivery_pending_cursor)
        self.assertEqual(c.crawl(), 5)

    def test_changed_scope_contract_and_malformed_positions_rejected(self):
        c = self.backfill(); c.crawl(limit=3); cursor = c.delivery_pending_cursor
        for change in ({'contract':'changed'}, {'offset':-1}, {'page':False}, {'page_hash':''}):
            c = self.backfill(dict(cursor, **change))
            self.assertEqual(c.crawl(), 0)
            self.assertEqual(c.delivery_source_failure, 'configuration')
            self.fetch.assert_not_called()

    def test_full_mode_keeps_native_rescan_behavior(self):
        c = self.backfill(); c.crawl(limit=3); cursor = c.delivery_pending_cursor
        c = self.crawler(); c.delivery_cursor = cursor
        self.assertEqual(c.crawl(), 5)
        self.assertIsNone(c.delivery_pending_cursor)
        self.assertEqual(self.fetch.call_args_list[0].args[1]['menuSn'], '15')


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')
class KrivetCursorPostgres(KrivetCursor):
    BACKEND = 'postgres'


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')
class KrivetCursorWorker(unittest.TestCase):
    def test_actual_worker_cancel_resume_and_auto_chain(self):
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs, worker
        case = KrivetCursorPostgres(methodName='test_partial_page_survives_new_crawler_across_all_four_menus')
        try:
            case.setUp(); init_delivery_schema(case.conn)
            class Factory:
                DELIVERY_CURSOR_VERSION = case.module.KrivetReKrKorCrawler.DELIVERY_CURSOR_VERSION
                DELIVERY_ORDER = 'arbitrary'
                def __new__(cls, **kwargs):
                    return case.crawler()
            jobs.enqueue_job(case.conn, case.SITE, mode='backfill', limit_n=3)
            self.assertEqual(worker.run_job(case.conn,jobs.claim_next_job(case.conn),{case.SITE:Factory},delay=0),3)
            self.assertEqual(jobs.load_cursor(case.conn,case.SITE)['offset'],1)
            job=jobs.claim_next_job(case.conn);before=case.count()
            def cancel():
                if case.count()>=before+1:
                    jobs.cancel_job(case.conn,job['id']);return True
                return False
            self.assertEqual(worker.run_job(case.conn,job,{case.SITE:Factory},delay=0,should_cancel=cancel),1)
            self.assertEqual(jobs.job_detail(case.conn,job['id'])['job']['status'],'cancelled')
            self.assertIsNone(jobs.claim_next_job(case.conn))
            jobs.enqueue_job(case.conn,case.SITE,mode='backfill',limit_n=3)
            counts=[]
            for _ in range(8):
                job=jobs.claim_next_job(case.conn)
                if not job:break
                counts.append(worker.run_job(case.conn,job,{case.SITE:Factory},delay=0))
            self.assertEqual(counts,[3,1]);self.assertEqual(case.count(),8)
            progress=case.conn.execute('SELECT * FROM crawl_site_progress WHERE site_id=%s',(case.SITE,)).fetchone()
            self.assertIsNotNone(progress['completed_at'])
        finally:
            case.doCleanups()
