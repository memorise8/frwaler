"""Actual database coverage beyond bounded HAL listing chunks."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from crawler import db_libertree
from crawler.base_crawler import BaseCrawler, CrawlCancelled

ROOT = Path(__file__).resolve().parents[1]


class CheckpointContract:
    BACKEND = 'sqlite'

    def setUp(self):
        spec = importlib.util.spec_from_file_location('checkpoint_'+self.SITE, ROOT/'crawler/sites/custom'/f'{self.SITE}.py')
        self.module = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.module)
        self.cls = next(v for v in vars(self.module).values() if isinstance(v, type) and issubclass(v, BaseCrawler) and getattr(v, 'site_id', None) == self.SITE)
        backend = db_libertree
        if self.BACKEND == 'postgres':
            from crawler import db_pg
            backend = db_pg
            self.conn = backend.open_db(os.environ['TEST_PG_DSN'])
            self.conn.execute('DROP SCHEMA public CASCADE'); self.conn.execute('CREATE SCHEMA public'); self.conn.commit()
        else:
            self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close); backend.init_db(self.conn); backend.upsert_site(self.conn, self.SITE, self.SITE, 'https://hal.science')
        self.enterContext(patch.dict(os.environ, {'LIBERTREE_DB_BACKEND': self.BACKEND}))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(patch.object(self.module.time, 'sleep'))
        owner = self.module if hasattr(self.module, '_ROWS') else self.cls
        self.enterContext(patch.object(owner, '_ROWS' if owner is self.module else '_PAGE_SIZE', 3))
        self.enterContext(patch.object(owner, '_MAX_PAGES', 2))
        self.docs = [{'docid': n, 'halId_s': f'hal-{n}', 'uri_s': f'https://hal.science/hal-{n}',
                      'title_s': [f'Research {n}'], 'abstract_s': ['Scientific abstract. '*10]} for n in range(1, 10)]
        self.cursor = None

    def count(self):
        row = self.conn.execute('SELECT count(*) AS n FROM documents').fetchone()
        return row['n'] if isinstance(row, dict) else row[0]

    def run_chunk(self, limit=None, *, cancel_at=None, cap=None, fail_detail=None, repeat=False, empty_early=False):
        c = self.cls(self.conn); self.last = c; c.delivery_mode = 'backfill'; c.delivery_cursor = self.cursor
        offsets = []
        def fetch(start, rows=3):
            offsets.append(start)
            docs = [] if empty_early else self.docs[(0 if repeat else start):(0 if repeat else start)+min(rows, cap or rows)]
            return {'response': {'numFound': len(self.docs), 'docs': docs}}
        def detail(item):
            if item['docid'] == fail_detail:
                raise RuntimeError('controlled detail failure')
            return item
        if cancel_at is not None:
            c.delivery_should_cancel = lambda: self.count() >= cancel_at
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(c, '_fetch_page' if hasattr(c, '_fetch_page') else '_fetch_list', side_effect=fetch))
                if hasattr(c, '_fetch_detail'):
                    stack.enter_context(patch.object(c, '_fetch_detail', side_effect=detail))
                returned = c.crawl(limit)
                return returned, offsets
        finally:
            self.cursor = c.delivery_pending_cursor or self.cursor
            c._session.close()

    def test_page_cap_resumes_to_terminal_without_false_completion(self):
        self.assertEqual(self.run_chunk()[0], 6)
        self.assertFalse(self.last.delivery_exhausted)
        self.assertIsNotNone(self.cursor)
        self.assertEqual(self.run_chunk()[0], 3)
        self.assertTrue(self.last.delivery_exhausted)
        self.assertEqual(self.count(), 9)

    def test_new_limit_mid_page_checkpoints_without_skipping_remainder(self):
        for expected in (2, 2, 2, 2, 1):
            self.assertEqual(self.run_chunk(limit=2)[0], expected)
        self.assertTrue(self.last.delivery_exhausted)
        self.assertEqual(self.count(), 9)

    def test_cancel_resumes_after_saved_item(self):
        with self.assertRaises(CrawlCancelled): self.run_chunk(cancel_at=2)
        self.assertEqual(self.count(), 2)
        self.assertIsNotNone(self.cursor)
        self.assertEqual(self.run_chunk()[0], 6)
        self.assertEqual(self.run_chunk()[0], 1)
        self.assertTrue(self.last.delivery_exhausted)

    def test_changed_resume_anchor_stops_before_more_saves(self):
        self.run_chunk(limit=2)
        self.docs[0], self.docs[2] = self.docs[2], self.docs[0]
        self.assertEqual(self.run_chunk()[0], 0)
        self.assertEqual(self.last.delivery_source_failure, 'invalid_response')
        self.assertEqual(self.count(), 2)
        self.assertFalse(self.last.delivery_exhausted)

    def test_short_pages_are_not_terminal_and_filtered_pages_advance(self):
        for doc in self.docs[:4]: doc['abstract_s'] = ['short']
        for _ in range(6):
            self.run_chunk(cap=2)
            if self.last.delivery_exhausted: break
        self.assertEqual(self.count(), 5)
        self.assertTrue(self.last.delivery_exhausted)

    def test_empty_before_reported_total_is_failure(self):
        self.run_chunk(empty_early=True)
        self.assertEqual(self.last.delivery_source_failure, 'invalid_response')
        self.assertFalse(self.last.delivery_exhausted)

    def test_repeated_response_fails_without_advancing_past_it(self):
        self.assertEqual(self.run_chunk(repeat=True)[0], 3)
        self.assertEqual(self.last.delivery_source_failure, 'invalid_response')
        self.assertFalse(self.last.delivery_exhausted)

    def test_failed_save_does_not_skip_failed_item_on_resume(self):
        original = self.cls._save_paper
        def save(c, paper):
            if paper['title'] == 'Research 2': raise RuntimeError('controlled persistence failure')
            return original(c, paper)
        with patch.object(self.cls, '_save_paper', save):
            self.assertEqual(self.run_chunk()[0], 1)
        self.assertEqual(self.last.delivery_source_failure, 'item_failed')
        for _ in range(3):
            self.run_chunk()
            if self.last.delivery_exhausted: break
        self.assertEqual(self.count(), 9)
        self.assertTrue(self.last.delivery_exhausted)

    def test_changed_total_during_resume_stops_before_more_saves(self):
        self.run_chunk(limit=2)
        self.docs.pop()
        self.assertEqual(self.run_chunk()[0], 0)
        self.assertEqual(self.last.delivery_source_failure, 'invalid_response')
        self.assertEqual(self.count(), 2)

    def test_invalid_cursor_fails_before_network(self):
        self.cursor = {'version': 'hal-offset-anchor-v1', 'offset': 999}
        self.assertEqual(self.run_chunk(), (0, []))
        self.assertEqual(self.last.delivery_source_failure, 'configuration')

    def test_backfill_capability_is_limited_and_requires_trial(self):
        from delivery.capabilities import capabilities
        cap = capabilities(self.SITE)
        self.assertTrue(cap['modes']['backfill']['allowed'])
        self.assertTrue(cap['modes']['backfill']['requires_trial'])
        self.assertEqual(cap['verification']['live'], 'not_verified')

    def test_worker_persists_cursor_requeues_and_marks_only_terminal_complete(self):
        if self.BACKEND != 'postgres': self.skipTest('requires PostgreSQL worker lifecycle')
        from delivery.db import schema
        from delivery.worker import jobs, worker
        schema.init_delivery_schema(self.conn)
        jobs.enqueue_job(self.conn, self.SITE, mode='backfill', limit_n=2)
        fetch_name = '_fetch_page' if hasattr(self.cls, '_fetch_page') else '_fetch_list'
        def fetch(c, start, rows=3):
            return {'response': {'numFound': len(self.docs), 'docs': self.docs[start:start+rows]}}
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(self.cls, fetch_name, fetch))
            if hasattr(self.cls, '_fetch_detail'):
                stack.enter_context(patch.object(self.cls, '_fetch_detail', lambda c, item: item))
            for expected in (2, 2, 2, 2, 1):
                job = jobs.claim_next_job(self.conn)
                self.assertIsNotNone(job)
                self.assertEqual(worker.run_job(self.conn, job, {self.SITE: self.cls}, delay=0), expected)
                progress = self.conn.execute('SELECT completed_at FROM crawl_site_progress WHERE site_id=%s', (self.SITE,)).fetchone()
                if self.count() < len(self.docs): self.assertIsNone(progress['completed_at'])
            self.assertIsNotNone(progress['completed_at'])
            self.assertIsNone(jobs.claim_next_job(self.conn))
        self.assertEqual(self.count(), 9)


for site in ('envt', 'inria', 'polytechnique'):
    attrs = {'SITE': site+'-hal-science-search'}
    globals()['SQLite_'+site] = type('SQLite_'+site, (CheckpointContract, unittest.TestCase), attrs)
    pg = type('Postgres_'+site, (CheckpointContract, unittest.TestCase), dict(attrs, BACKEND='postgres'))
    globals()['Postgres_'+site] = unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')(pg)
del pg
