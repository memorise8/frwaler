import hashlib
import importlib.util
import json
import os
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from crawler import db, db_libertree
from crawler.base_crawler import CrawlCancelled

ROOT = Path(__file__).resolve().parents[1]
F = ROOT / 'tests/fixtures/cmi_cursor'
SPEC = importlib.util.spec_from_file_location('cmi_cursor', ROOT / 'crawler/sites/custom/cmi-no-publications.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)
C = M.CmiNoPublicationsCrawler


class CmiCursorTests(unittest.TestCase):
    def setUp(self):
        sources = json.loads((F / 'sources.json').read_text())['sources']
        self.lookup = {x['url']: (F / x['file']).read_text() for x in sources}
        self.urls = {x['file']: x['url'] for x in sources}
        self.lookup['https://www.cmi.no/publications/search?page=1&pubtype=journal-articles'] = (F / 'list-first.html').read_text()
        self.lookup['https://www.cmi.no/publications/search?page=55&pubtype=journal-articles'] = (F / 'list-last.html').read_text()

    def crawler(self, kind='legacy', conn=None):
        if conn is None:
            conn = sqlite3.connect(':memory:')
            conn.row_factory = sqlite3.Row
            self.addCleanup(conn.close)
            if kind == 'legacy':
                db.init_db(conn)
                db.register_site(conn, C.site_id, C.site_id, C.base_url)
            else:
                db_libertree.init_db(conn)
                conn.execute('INSERT INTO sites(site_id,site_name,site_url) VALUES(?,?,?)',
                             (C.site_id, C.site_id, C.base_url))
                conn.commit()
        return conn, C(conn, delay=0)

    def run_crawl(self, obj, **kwargs):
        with patch.object(M, '_curl_get', side_effect=self.lookup.__getitem__):
            return obj.crawl(**kwargs)

    def test_cancel_and_resume_next_actual_article_both_schemas(self):
        for kind in ['legacy', 'v2']:
            conn, obj = self.crawler(kind)
            obj.delivery_should_cancel = lambda: conn.execute('SELECT count(*) FROM documents').fetchone()[0] >= 1
            with self.assertRaises(CrawlCancelled):
                self.run_crawl(obj)
            before = dict(conn.execute('SELECT * FROM documents').fetchone())
            self.assertEqual(obj.delivery_pending_cursor['next_index'], 1)
            self.assertFalse(obj.delivery_exhausted)
            _, resumed = self.crawler(conn=conn)
            resumed.delivery_cursor = obj.delivery_pending_cursor
            with patch.object(M, '_curl_get', side_effect=self.lookup.__getitem__) as fetch:
                self.assertEqual(resumed.crawl(limit=1), 1)
            self.assertEqual([x.args[0] for x in fetch.call_args_list],
                             ['https://www.cmi.no/publications/search?page=1&pubtype=journal-articles',
                              self.urls['detail-second.html']])
            self.assertEqual(before, dict(conn.execute('SELECT * FROM documents').fetchone()))
            self.assertEqual(resumed.delivery_pending_cursor['next_index'], 2)

    def test_actual_last_empty_abstract_saved_before_completion(self):
        for kind in ['legacy', 'v2']:
            conn, obj = self.crawler(kind)
            items, last = obj._source_page(M._make_soup((F / 'list-last.html').read_text()), 55)
            self.assertEqual((len(items), last), (15, 55))
            self.assertEqual(items[-1][1], '1605')
            obj.delivery_cursor = dict(version=1, scope='journal_articles', page=55, next_index=14,
                                       last_page=55, list_digest=hashlib.sha256(json.dumps(items).encode()).hexdigest())
            self.assertEqual(self.run_crawl(obj), 1)
            row = dict(conn.execute('SELECT * FROM documents').fetchone())
            self.assertFalse(row['abstract'])
            self.assertTrue(obj.delivery_exhausted)
            _, resumed = self.crawler(conn=conn)
            resumed.delivery_cursor = obj.delivery_pending_cursor
            with patch.object(M, '_curl_get', side_effect=AssertionError('unexpected fetch')):
                self.assertEqual(resumed.crawl(), 0)
            self.assertTrue(resumed.delivery_exhausted)
            self.assertEqual(row, dict(conn.execute('SELECT * FROM documents').fetchone()))

    def test_changed_list_blocks_resume_without_advancing(self):
        conn, obj = self.crawler()
        self.assertEqual(self.run_crawl(obj, limit=1), 1)
        _, resumed = self.crawler(conn=conn)
        resumed.delivery_cursor = obj.delivery_pending_cursor
        key = 'https://www.cmi.no/publications/search?page=1&pubtype=journal-articles'
        self.lookup[key] = self.lookup[key].replace('/publications/10057-', '/publications/999999-')
        with self.assertRaisesRegex(ValueError, 'source list changed'):
            self.run_crawl(resumed)
        self.assertFalse(resumed.delivery_exhausted)
        self.assertIsNone(resumed.delivery_pending_cursor)
        self.assertEqual(conn.execute('SELECT count(*) FROM documents').fetchone()[0], 1)

    def test_failed_detail_preserves_first_checkpoint(self):
        conn, obj = self.crawler()
        self.lookup[self.urls['detail-second.html']] = ''
        with self.assertRaisesRegex(ValueError, 'detail could not be parsed'):
            self.run_crawl(obj)
        self.assertEqual(obj.delivery_pending_cursor['next_index'], 1)
        self.assertEqual(obj.delivery_source_failure, 'item_failed')
        self.assertFalse(obj.delivery_exhausted)
        self.assertEqual(conn.execute('SELECT count(*) FROM documents').fetchone()[0], 1)

    def test_save_failure_does_not_skip_unsaved_article(self):
        conn, obj = self.crawler()
        save = obj._save_paper
        def fail_second(paper):
            if paper['external_id'] == '10017':
                raise RuntimeError('owned save failure')
            return save(paper)
        with patch.object(obj, '_save_paper', side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, 'owned save failure'):
                self.run_crawl(obj)
        before = dict(conn.execute('SELECT * FROM documents').fetchone())
        self.assertEqual(obj.delivery_pending_cursor['next_index'], 1)
        _, resumed = self.crawler(conn=conn)
        resumed.delivery_cursor = obj.delivery_pending_cursor
        self.assertEqual(self.run_crawl(resumed, limit=1), 1)
        self.assertEqual(before, dict(conn.execute('SELECT * FROM documents').fetchone()))

    def test_clamped_or_missing_pagination_cannot_mark_done(self):
        _, obj = self.crawler()
        for raw, page in [((F / 'list-last.html').read_text(), 56), ('<html>No items</html>', 1)]:
            with self.assertRaises(ValueError):
                obj._source_page(BeautifulSoup(raw, 'lxml'), page)
        self.assertFalse(obj.delivery_exhausted)

    def test_zero_limit_budget_and_cancel_do_not_fetch_or_finish(self):
        for mode in ['limit', 'budget', 'cancel']:
            _, obj = self.crawler()
            with patch.object(M, '_curl_get', side_effect=AssertionError('unexpected fetch')):
                if mode == 'limit':
                    self.assertEqual(obj.crawl(limit=0), 0)
                elif mode == 'budget':
                    with patch.dict(os.environ, LIBERTREE_MAX_WALL_S='0'):
                        self.assertEqual(obj.crawl(), 0)
                else:
                    obj.delivery_should_cancel = lambda: True
                    with self.assertRaises(CrawlCancelled):
                        obj.crawl()
            self.assertFalse(obj.delivery_exhausted)

    def test_inconsistent_done_cursor_cannot_mark_complete(self):
        _, obj = self.crawler()
        obj.delivery_cursor = dict(version=1, scope='done', page=55, last_page=54, next_index=0)
        with self.assertRaisesRegex(ValueError, 'invalid CMI source cursor'):
            obj.crawl()
        self.assertFalse(obj.delivery_exhausted)


if __name__ == '__main__':
    unittest.main()
