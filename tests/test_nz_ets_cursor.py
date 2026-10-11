import contextlib
import hashlib
import importlib.util
import io
import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from crawler import db, db_libertree, storage
from crawler.base_crawler import CrawlCancelled

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures/nz_ets_cursor'
SPEC = importlib.util.spec_from_file_location(
    'nz_ets_cursor', ROOT / 'crawler/sites/custom/consult-environment-govt-nz-climate.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
Crawler = MODULE.ConsultEnvironmentGovtNzClimateCrawler


class NzEtsCursorTests(unittest.TestCase):
    def setUp(self):
        sources = json.loads((FIXTURES / 'sources.json').read_text())['sources']
        self.sources = {x['file']: x for x in sources}
        self.lookup = {x['url']: (FIXTURES / x['file']).read_text() for x in sources}
        self.first_url = self.sources['list-first.html']['url']
        self.last_url = self.sources['list-last.html']['url']

    def crawler(self, kind='legacy', conn=None):
        if conn is None:
            conn = sqlite3.connect(':memory:')
            conn.row_factory = sqlite3.Row
            self.addCleanup(conn.close)
            if kind == 'legacy':
                db.init_db(conn)
                db.register_site(conn, Crawler.site_id, Crawler.site_id, Crawler.base_url)
            else:
                db_libertree.init_db(conn)
                conn.execute('INSERT INTO sites(site_id,site_name,site_url) VALUES(?,?,?)',
                             (Crawler.site_id, Crawler.site_id, Crawler.base_url))
                conn.commit()
        return conn, Crawler(conn, delay=0)

    def run_crawl(self, obj, **kwargs):
        with patch.object(obj, '_fetch_html', side_effect=self.lookup.__getitem__), \
                patch.object(storage, 'cleanup_doc_files', side_effect=AssertionError('unexpected cleanup')), \
                contextlib.redirect_stdout(io.StringIO()):
            return obj.crawl(**kwargs)

    def test_cancel_after_save_resumes_next_actual_response_both_schemas(self):
        for kind in ['legacy', 'v2']:
            conn, obj = self.crawler(kind)
            obj.delivery_should_cancel = lambda: conn.execute('SELECT COUNT(*) FROM documents').fetchone()[0] >= 1
            with self.assertRaises(CrawlCancelled):
                self.run_crawl(obj)
            prior = dict(conn.execute('SELECT * FROM documents').fetchone())
            self.assertEqual(obj.delivery_pending_cursor['next_index'], 1)
            _, resumed = self.crawler(kind, conn)
            resumed.delivery_cursor = obj.delivery_pending_cursor
            with patch.object(resumed, '_fetch_html', side_effect=self.lookup.__getitem__) as fetch:
                self.assertEqual(resumed.crawl(limit=1), 1)
            self.assertEqual([c.args[0] for c in fetch.call_args_list],
                             [self.first_url, self.sources['detail-second.html']['url']])
            self.assertEqual(prior, dict(conn.execute('SELECT * FROM documents').fetchone()))
            self.assertEqual(resumed.delivery_pending_cursor['next_index'], 2)
            self.assertFalse(resumed.delivery_exhausted)

    def test_actual_last_response_finishes_only_after_save(self):
        conn, obj = self.crawler()
        ids, next_url = obj._parse_source_list(self.lookup[self.last_url], self.last_url)
        self.assertEqual(len(ids), 31)
        self.assertIsNone(next_url)
        obj.delivery_cursor = dict(version=1, scope='nz_ets_2025', list_url=self.last_url,
                                   next_index=30, list_digest=hashlib.sha256(json.dumps(ids).encode()).hexdigest())
        self.assertEqual(self.run_crawl(obj), 1)
        self.assertTrue(obj.delivery_exhausted)
        self.assertEqual(obj.delivery_pending_cursor['scope'], 'done')
        _, resumed = self.crawler(conn=conn)
        resumed.delivery_cursor = obj.delivery_pending_cursor
        with patch.object(resumed, '_fetch_html', side_effect=AssertionError('unexpected request')):
            self.assertEqual(resumed.crawl(), 0)
        self.assertTrue(resumed.delivery_exhausted)

    def test_changed_source_order_requires_overlap_review(self):
        conn, obj = self.crawler()
        self.assertEqual(self.run_crawl(obj, limit=1), 1)
        _, resumed = self.crawler(conn=conn)
        resumed.delivery_cursor = obj.delivery_pending_cursor
        soup = BeautifulSoup(self.lookup[self.first_url], 'lxml')
        anchor = soup.select_one('a[href*="view_respondent"]')
        anchor['href'] = anchor['href'].replace('640885614', '999999999')
        self.lookup[self.first_url] = str(soup)
        with self.assertRaisesRegex(ValueError, 'source list changed'):
            self.run_crawl(resumed)
        self.assertFalse(resumed.delivery_exhausted)
        self.assertEqual(resumed.delivery_source_failure, 'invalid_response')
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM documents').fetchone()[0], 1)

    def test_failed_detail_does_not_advance_or_complete(self):
        conn, obj = self.crawler()
        self.lookup[self.sources['detail-first.html']['url']] = None
        with self.assertRaisesRegex(ValueError, 'empty published response detail'):
            self.run_crawl(obj)
        self.assertIsNone(obj.delivery_pending_cursor)
        self.assertFalse(obj.delivery_exhausted)
        self.assertEqual(obj.delivery_source_failure, 'fetch_failed')
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM documents').fetchone()[0], 0)

    def test_save_then_failure_replay_keeps_committed_identity(self):
        conn, obj = self.crawler()
        save = obj._save_paper
        def fail_after_save(paper):
            save(paper)
            raise RuntimeError('failure before cursor advancement')
        with patch.object(obj, '_save_paper', side_effect=fail_after_save):
            with self.assertRaises(RuntimeError):
                self.run_crawl(obj)
        self.assertIsNone(obj.delivery_pending_cursor)
        prior = dict(conn.execute('SELECT * FROM documents').fetchone())
        _, resumed = self.crawler(conn=conn)
        self.assertEqual(self.run_crawl(resumed, limit=1), 1)
        self.assertEqual(prior, dict(conn.execute('SELECT * FROM documents').fetchone()))
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM documents').fetchone()[0], 2)

    def test_bad_catalogue_or_backwards_page_is_not_exhaustion(self):
        _, obj = self.crawler()
        with self.assertRaisesRegex(ValueError, 'catalogue'):
            obj._parse_source_list('<html>Unavailable</html>', self.first_url)
        soup = BeautifulSoup(self.lookup[self.first_url], 'lxml')
        soup.select_one('a[aria-label="Next page"]')['href'] = self.first_url
        with self.assertRaisesRegex(ValueError, 'does not advance'):
            obj._parse_source_list(str(soup), self.first_url)

    def test_cancel_zero_limit_and_expired_budget_before_request(self):
        _, obj = self.crawler()
        with patch.object(obj, '_fetch_html', side_effect=AssertionError('unexpected request')):
            self.assertEqual(obj.crawl(limit=0), 0)
            with patch.dict(MODULE.os.environ, {'LIBERTREE_MAX_WALL_S': '0'}):
                self.assertEqual(obj.crawl(), 0)
            self.assertFalse(obj.delivery_exhausted)
            obj.delivery_should_cancel = lambda: True
            with self.assertRaises(CrawlCancelled):
                obj.crawl()


if __name__ == '__main__':
    unittest.main()
