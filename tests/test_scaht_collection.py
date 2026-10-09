"""Exercise delivered SCAHT's real save path with source responses replaced."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from crawler import db_libertree
from crawler.base_crawler import CrawlCancelled

SOURCE = Path(__file__).resolve().parents[1]/'crawler/sites/custom/scaht-org-en.py'
spec = importlib.util.spec_from_file_location('scaht_collection_fixture', SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ScahtCollectionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        db_libertree.init_db(self.conn)
        db_libertree.upsert_site(self.conn, 'scaht-org-en', 'SCAHT', 'https://www.scaht.org')
        self.enterContext(patch.dict(os.environ, {'LIBERTREE_DB_BACKEND': 'sqlite'}))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(patch.object(module.time, 'sleep'))

    def crawler(self):
        c = module.SCAHTOrgEnCrawler(self.conn)
        c.delivery_mode = 'full'
        def listing(page, active):
            ids = {1: range(1, 6), 2: range(6, 9)}.get(page, [])
            records = [{'url': f'https://www.scaht.org/en/publications/{n}/', 'slug': str(n)} for n in ids]
            return (c.LIVE_LIST_URL, c.LIVE_LIST_URL, records, '?page=2' if page == 1 else None)
        def detail(record, label):
            return {'site_id': c.site_id, 'external_id': record['slug'],
                    'url': record['url'], 'title': 'Research '+record['slug'],
                    'abstract': 'Research abstract. ' * 10, 'published_date': '2026-06-05'}
        c._fetch_list_page = listing
        c._fetch_parse_save_item = detail
        return c

    def count(self):
        return self.conn.execute('SELECT count(*) FROM documents').fetchone()[0]

    def test_repeat_limits_reach_remaining_records(self):
        for expected, returned in ((3, 3), (6, 3), (8, 2), (8, 0)):
            c = self.crawler()
            self.assertEqual(c.crawl(limit=3), returned)
            self.assertEqual(self.count(), expected)
        self.assertEqual(self.conn.execute('SELECT count(DISTINCT post_number) FROM documents').fetchone()[0], 8)

    def test_cancel_on_known_rows_then_restart_preserves_all(self):
        self.crawler().crawl(limit=3)
        c = self.crawler()
        checked = []
        def cancel():
            checked.append(1)
            return len(checked) >= 2
        c.delivery_should_cancel = cancel
        with self.assertRaises(CrawlCancelled):
            c.crawl(limit=3)
        self.assertEqual(self.count(), 3)
        self.assertEqual(self.crawler().crawl(), 5)
        self.assertEqual(self.count(), 8)


if __name__ == '__main__':
    unittest.main()
