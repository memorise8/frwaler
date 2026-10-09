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


class ScahtDateTests(unittest.TestCase):
    BACKEND = 'sqlite'

    def setUp(self):
        backend = db_libertree
        if self.BACKEND == 'postgres':
            from crawler import db_pg
            backend = db_pg
            self.conn = backend.open_db(os.environ['TEST_PG_DSN'])
            self.conn.execute('DROP SCHEMA public CASCADE')
            self.conn.execute('CREATE SCHEMA public')
            self.conn.commit()
        else:
            self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        backend.init_db(self.conn)
        backend.upsert_site(self.conn, 'scaht-org-en', 'SCAHT', 'https://www.scaht.org')
        self.enterContext(patch.dict(os.environ, {'LIBERTREE_DB_BACKEND': self.BACKEND}))

    def persist(self, source, slug):
        c = module.SCAHTOrgEnCrawler(self.conn)
        c.delivery_mode = 'full'
        record = {'url': f'https://www.scaht.org/en/publications/{slug}/', 'slug': slug,
                  'title': 'Research', 'listed_date_raw': '5 Jun 2026', 'listed_date': '2026-06-05'}
        detail = {'title': 'Research', 'published_date_raw': 'June 5, 2026', 'published_date': '2026-06-05'}
        with patch.object(c, '_fetch_scaht_detail', return_value=detail), patch.object(c, '_fetch_source_metadata', return_value=source):
            paper = c._fetch_parse_save_item(record, 'fixture')
        c._save_paper(paper)
        fields = ['published_date', 'published_date_raw', 'published_date_status', 'published_date_precision', 'listed_date', 'listed_date_raw']
        placeholder = '%s' if self.BACKEND == 'postgres' else '?'
        row = self.conn.execute('SELECT '+','.join(fields)+' FROM documents WHERE post_number='+placeholder, (slug,)).fetchone()
        return tuple(row[field] for field in fields) if isinstance(row, dict) else row

    def test_crossref_partial_dates_preserve_precision_through_real_save(self):
        c = module.SCAHTOrgEnCrawler(self.conn)
        for parts, raw, precision in (([2025], '2025', 'year'), ([2025, 7], '2025-07', 'month')):
            with self.subTest(parts=parts):
                source = c._crossref_message_to_source({'published': {'date-parts': [parts]}}, 'https://api.crossref.org/works/example')
                row = self.persist(source, raw)
                self.assertEqual(row[:4], (None, raw, 'partial', precision))
                self.assertEqual(row[4:], ('2026-06-05', '5 Jun 2026'))

    def test_publisher_ambiguous_and_invalid_dates_do_not_fall_back(self):
        for raw, status in (('03/04/2025', 'ambiguous'), ('2025-02-31', 'invalid'), ('2025', 'partial')):
            with self.subTest(raw=raw):
                c = module.SCAHTOrgEnCrawler(self.conn)
                html = f'<html><head><meta name="citation_publication_date" content="{raw}"></head><body>Research</body></html>'
                with patch.object(c, '_curl_get', return_value=(html, 'https://publisher.invalid/record', 200, 'text/html')):
                    source = c._fetch_publisher_metadata('https://publisher.invalid/record', 'fixture')
                row = self.persist(source, status)
                self.assertEqual(row[:3], (None, raw, status))

    def test_complete_date_normalizes_but_keeps_source_text(self):
        row = self.persist({'published_date_raw': 'February 3, 2025', 'published_date': '2025-02-03'}, 'complete')
        self.assertEqual(row[:4], ('2025-02-03', 'February 3, 2025', 'normalized', 'day'))


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')
class ScahtPostgresDateTests(ScahtDateTests):
    BACKEND = 'postgres'


if __name__ == '__main__':
    unittest.main()
