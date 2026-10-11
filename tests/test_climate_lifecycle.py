import contextlib
import importlib.util
import io
import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch
from crawler import db, db_libertree
from crawler.base_crawler import CrawlCancelled

ROOT = Path(__file__).resolve().parents[1]
F = ROOT / 'tests/fixtures/climate_lifecycle'
SPEC = importlib.util.spec_from_file_location('climate_lifecycle', ROOT / 'crawler/sites/custom/climateinstitute-ca-newsroom.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)

class ClimateLifecycleTests(unittest.TestCase):
    def crawler(self, kind='legacy'):
        conn = sqlite3.connect(':memory:'); conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        cls = M.ClimateInstituteNewsroomCrawler
        if kind == 'legacy':
            db.init_db(conn); db.register_site(conn, cls.site_id, cls.site_id, cls.base_url)
        else:
            db_libertree.init_db(conn)
            conn.execute('INSERT INTO sites(site_id,site_name,site_url) VALUES(?,?,?)', (cls.site_id, cls.site_id, cls.base_url)); conn.commit()
        return conn, cls(conn, delay=0)

    def run_source(self, obj, responses, limit=None):
        with patch.object(M, '_curl_get', side_effect=responses), patch.object(M.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()):
            return obj.crawl(limit=limit)

    def test_cancel_before_request_and_zero_limit(self):
        _, obj = self.crawler(); obj.delivery_should_cancel = lambda: True
        with patch.object(M, '_curl_get', side_effect=AssertionError('unexpected network')):
            self.assertEqual(obj.crawl(limit=0), 0)
            with self.assertRaises(CrawlCancelled): obj.crawl()
        self.assertFalse(obj.delivery_exhausted)

    def test_cancel_during_response_does_not_mark_empty_as_exhausted(self):
        _, obj = self.crawler()
        def response(*args, **kwargs):
            obj.delivery_should_cancel = lambda: True
            return '[]'
        with patch.object(M, '_curl_get', side_effect=response):
            with self.assertRaises(CrawlCancelled): obj.crawl()
        self.assertFalse(obj.delivery_exhausted)

    def test_actual_records_cancel_resume_limit_repeat_preserve_rows_both_schemas(self):
        posts = (F/'posts.json').read_text(); boundary = (F/'boundary.json').read_text()
        for kind in ['legacy', 'v2']:
            conn, obj = self.crawler(kind)
            obj.delivery_should_cancel = lambda: conn.execute('SELECT COUNT(*) FROM documents').fetchone()[0] >= 1
            with self.assertRaises(CrawlCancelled): self.run_source(obj, [posts, boundary])
            before = dict(conn.execute('SELECT * FROM documents').fetchone())
            obj.delivery_should_cancel = lambda: False
            self.assertEqual(self.run_source(obj, [posts], limit=2), 2)
            self.assertFalse(obj.delivery_exhausted)
            self.assertEqual(self.run_source(obj, [posts, boundary]), 1)
            self.assertTrue(obj.delivery_exhausted)
            rows = [dict(x) for x in conn.execute('SELECT * FROM documents')]
            self.assertEqual(len(rows), 4); self.assertEqual(rows[0], before)
            self.assertEqual(self.run_source(obj, [posts, boundary]), 0)
            self.assertEqual(rows, [dict(x) for x in conn.execute('SELECT * FROM documents')])

    def test_failure_responses_are_not_exhaustion(self):
        cases = [(None, 'fetch_failed'), ('not JSON', 'invalid_response'), ('123', 'invalid_response'),
                 (json.dumps({'code':'rest_forbidden','data':{'status':403}}), 'authentication_failed'),
                 (json.dumps({'code':'rate_limit','data':{'status':429}}), 'rate_limited'),
                 ((F/'boundary.json').read_text(), 'invalid_response')]
        for response, reason in cases:
            _, obj = self.crawler()
            self.assertEqual(self.run_source(obj, [response]), 0)
            self.assertEqual(obj.delivery_source_failure, reason)
            self.assertFalse(obj.delivery_exhausted)

    def test_item_failure_then_valid_boundary_stays_incomplete(self):
        conn, obj = self.crawler()
        with patch.object(obj, '_save_paper', side_effect=ValueError('owned DB write failure')):
            self.assertEqual(self.run_source(obj, [(F/'posts.json').read_text(), (F/'boundary.json').read_text()]), 0)
        self.assertEqual(obj.delivery_source_failure, 'item_failed')
        self.assertFalse(obj.delivery_exhausted)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM documents').fetchone()[0], 0)

    def test_excluded_short_body_does_not_claim_full_collection(self):
        _, obj = self.crawler(); posts = json.loads((F/'posts.json').read_text())
        posts[0]['content']['rendered'] = 'short'
        posts[0]['excerpt']['rendered'] = 'short'
        self.assertEqual(self.run_source(obj, [json.dumps(posts), (F/'boundary.json').read_text()]), 3)
        self.assertEqual(obj.delivery_source_failure, 'item_failed')
        self.assertFalse(obj.delivery_exhausted)

    def test_repeated_page_does_not_prove_end(self):
        _, obj = self.crawler(); posts = (F/'posts.json').read_text()
        self.assertEqual(self.run_source(obj, [posts, posts]), 4)
        self.assertFalse(obj.delivery_exhausted)
        self.assertEqual(obj.delivery_source_failure, 'invalid_response')

if __name__ == '__main__': unittest.main()
