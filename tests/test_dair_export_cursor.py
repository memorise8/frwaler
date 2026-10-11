import copy
import gzip
import hashlib
import importlib.util
import json
import os
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from crawler import db, db_libertree
from crawler.base_crawler import CrawlCancelled

ROOT = Path(__file__).resolve().parents[1]
F = ROOT / 'tests/fixtures/dair_export'
SPEC = importlib.util.spec_from_file_location('dair_export_cursor', ROOT / 'crawler/sites/custom/dair-dias-ie-cgi.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)
C = M.DairDiasIeCgiCrawler
RAW = gzip.decompress((F / 'all1054.json.gz').read_bytes()).decode()
RECORDS = json.loads(RAW)
DIGEST = hashlib.sha256(json.dumps(RECORDS, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class DairExportTests(unittest.TestCase):
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

    def run_crawl(self, obj, raw=RAW, **kwargs):
        with patch.object(obj, '_curl_get', return_value=raw):
            return obj.crawl(**kwargs)

    def test_all1054_real_articles_keep_empty_short_abstracts_and_PDFs(self):
        _, obj = self.crawler()
        papers = [obj._record_to_paper(x) for x in RECORDS]
        self.assertEqual(len(papers), 1054)
        self.assertEqual([p['external_id'] for p in papers], [str(x['eprintid']) for x in RECORDS])
        self.assertEqual(sum(not p['abstract'] for p in papers), 296)
        self.assertEqual(sum(0 < len(p['abstract']) < 50 for p in papers), 2)
        self.assertEqual(sum(bool(p['pdf_url']) for p in papers), 1039)
        for source, paper in zip(RECORDS, papers):
            self.assertEqual(paper['abstract'], (source.get('abstract') or '').strip())

    def test_cancel_resume_preserves_rows_both_schemas(self):
        for kind in ['legacy', 'v2']:
            conn, obj = self.crawler(kind)
            obj.delivery_should_cancel = lambda: conn.execute('SELECT count(*) FROM documents').fetchone()[0] >= 2
            with self.assertRaises(CrawlCancelled):
                self.run_crawl(obj)
            before = [dict(x) for x in conn.execute('SELECT * FROM documents')]
            self.assertEqual(obj.delivery_pending_cursor['next_index'], 2)
            self.assertFalse(obj.delivery_exhausted)
            _, resumed = self.crawler(conn=conn)
            resumed.delivery_cursor = obj.delivery_pending_cursor
            self.assertEqual(self.run_crawl(resumed, limit=1), 1)
            self.assertEqual(before, [dict(x) for x in conn.execute('SELECT * FROM documents')][:2])
            self.assertEqual(resumed.delivery_pending_cursor['next_index'], 3)

    def test_real_last_article_without_abstract_is_saved_before_done(self):
        for kind in ['legacy', 'v2']:
            conn, obj = self.crawler(kind)
            self.assertEqual(RECORDS[-1]['eprintid'], 51)
            obj.delivery_cursor = dict(version=1, scope='articles', next_index=1053,
                                       total=1054, export_digest=DIGEST)
            self.assertEqual(self.run_crawl(obj), 1)
            self.assertTrue(obj.delivery_exhausted)
            row = dict(conn.execute('SELECT * FROM documents').fetchone())
            self.assertFalse(row['abstract'])
            _, resumed = self.crawler(conn=conn)
            resumed.delivery_cursor = obj.delivery_pending_cursor
            with patch.object(resumed, '_curl_get', side_effect=AssertionError('unexpected fetch')):
                self.assertEqual(resumed.crawl(), 0)
            self.assertTrue(resumed.delivery_exhausted)
            self.assertEqual(row, dict(conn.execute('SELECT * FROM documents').fetchone()))

    def test_failed_save_keeps_position_before_unsaved_article(self):
        conn, obj = self.crawler()
        save = obj._save_paper
        def fail(paper):
            if paper['external_id'] == str(RECORDS[1]['eprintid']):
                raise RuntimeError('owned save failure')
            return save(paper)
        with patch.object(obj, '_save_paper', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'owned save failure'):
                self.run_crawl(obj)
        self.assertEqual(obj.delivery_pending_cursor['next_index'], 1)
        self.assertFalse(obj.delivery_exhausted)
        before = dict(conn.execute('SELECT * FROM documents').fetchone())
        _, resumed = self.crawler(conn=conn)
        resumed.delivery_cursor = obj.delivery_pending_cursor
        self.assertEqual(self.run_crawl(resumed, limit=1), 1)
        self.assertEqual(before, dict(conn.execute('SELECT * FROM documents').fetchone()))

    def test_failed_parse_cannot_advance_cursor_or_finish(self):
        conn, obj = self.crawler()
        convert = obj._record_to_paper
        with patch.object(obj, '_record_to_paper', side_effect=lambda r: None if r['eprintid'] == RECORDS[1]['eprintid'] else convert(r)):
            with self.assertRaisesRegex(ValueError, 'could not be parsed'):
                self.run_crawl(obj)
        self.assertEqual(obj.delivery_pending_cursor['next_index'], 1)
        self.assertEqual(conn.execute('SELECT count(*) FROM documents').fetchone()[0], 1)
        self.assertEqual(obj.delivery_source_failure, 'item_failed')
        self.assertFalse(obj.delivery_exhausted)

    def test_changed_export_cannot_silently_resume(self):
        conn, obj = self.crawler()
        self.assertEqual(self.run_crawl(obj, limit=1), 1)
        changed = copy.deepcopy(RECORDS)
        changed[0]['title'] += ' changed'
        _, resumed = self.crawler(conn=conn)
        resumed.delivery_cursor = obj.delivery_pending_cursor
        with self.assertRaisesRegex(ValueError, 'export changed'):
            self.run_crawl(resumed, raw=json.dumps(changed))
        self.assertIsNone(resumed.delivery_pending_cursor)
        self.assertFalse(resumed.delivery_exhausted)
        self.assertEqual(conn.execute('SELECT count(*) FROM documents').fetchone()[0], 1)

    def test_scope_identity_and_duplicate_errors_prevent_all_writes(self):
        for change in ['scope', 'identity', 'duplicate']:
            with self.subTest(change=change):
                conn, obj = self.crawler()
                changed = copy.deepcopy(RECORDS)
                if change == 'scope':
                    changed[1]['type'] = 'book'
                elif change == 'identity':
                    changed[1]['eprintid'] = None
                else:
                    changed[1]['eprintid'] = changed[0]['eprintid']
                with self.assertRaises(ValueError):
                    self.run_crawl(obj, raw=json.dumps(changed))
                self.assertEqual(conn.execute('SELECT count(*) FROM documents').fetchone()[0], 0)
                self.assertFalse(obj.delivery_exhausted)

    def test_missing_malformed_or_empty_export_cannot_finish(self):
        for raw in [None, 'bad json', '{}', '[]']:
            _, obj = self.crawler()
            with self.assertRaisesRegex(ValueError, 'missing or empty'):
                self.run_crawl(obj, raw=raw)
            self.assertFalse(obj.delivery_exhausted)

    def test_zero_limit_budget_or_cancel_does_not_fetch(self):
        for mode in ['limit', 'budget', 'cancel']:
            _, obj = self.crawler()
            with patch.object(obj, '_curl_get', side_effect=AssertionError('unexpected fetch')):
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

    def test_invalid_checkpoint_cannot_mark_done(self):
        for cursor in [dict(version=True), dict(scope='done', next_index=1, total=2, export_digest=DIGEST),
                       dict(scope='articles', next_index=-1), dict(scope='done', next_index=0, total=0)]:
            _, obj = self.crawler()
            obj.delivery_cursor = cursor
            with self.assertRaisesRegex(ValueError, 'invalid DAIR'):
                obj.crawl()
            self.assertFalse(obj.delivery_exhausted)


if __name__ == '__main__':
    unittest.main()
