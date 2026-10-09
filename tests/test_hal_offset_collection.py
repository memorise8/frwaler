"""Real save paths for HAL adapters; controlled API windows expose source gaps."""
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
SITES = ('artsetmetiers', 'enscp', 'enssib', 'icn', 'icp', 'ineris', 'ird', 'polytechnique')


class HalOffsetContract:
    BACKEND = 'sqlite'

    def setUp(self):
        spec = importlib.util.spec_from_file_location('hal_fixture_'+self.SITE.replace('-', '_'), ROOT/'crawler/sites/custom'/f'{self.SITE}.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.cls = next(v for v in vars(self.module).values() if isinstance(v, type) and issubclass(v, BaseCrawler) and getattr(v, 'site_id', None) == self.SITE)
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
        backend.upsert_site(self.conn, self.SITE, self.SITE, 'https://hal.science')
        self.enterContext(patch.dict(os.environ, {'LIBERTREE_DB_BACKEND': self.BACKEND}))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(patch.object(self.module.time, 'sleep'))
        self.enterContext(patch.object(self.module, '_ROWS', 3))

    def count(self):
        row = self.conn.execute('SELECT count(*) AS n FROM documents').fetchone()
        return row['n'] if isinstance(row, dict) else row[0]

    def crawl(self, limit=None, *, filtered=(), response_cap=None, cancel_at=None):
        records = [{'docid': n+1, 'halId_s': f'hal-{n+1}', 'uri_s': f'https://hal.science/hal-{n+1}',
                    'title_s': [f'Scientific research {n+1}'], 'abstract_s': ['short' if n in filtered else 'Scientific abstract. '*10]} for n in range(9)]
        c = self.cls(self.conn)
        c.delivery_mode = 'full'
        if cancel_at is not None:
            c.delivery_should_cancel = lambda: self.count() >= cancel_at
        offsets = []
        def fetch(start, size):
            offsets.append(start)
            size = min(size, response_cap) if response_cap else size
            return {'response': {'numFound': len(records), 'docs': records[start:start+size]}}
        with patch.object(c, '_fetch_page', side_effect=fetch):
            returned = c.crawl(limit=limit)
        return returned, offsets

    def test_small_new_limit_reaches_remainder_on_repeated_runs(self):
        for total in (2, 4, 6, 8):
            self.assertEqual(self.crawl(limit=2)[0], 2)
            self.assertEqual(self.count(), total)
        self.assertEqual(self.crawl(limit=2)[0], 1)
        self.assertEqual(self.count(), 9)

    def test_filtered_whole_page_is_not_source_end(self):
        returned, offsets = self.crawl(filtered=(3, 4, 5))
        self.assertEqual((returned, self.count()), (6, 6))
        self.assertIn(6, offsets)

    def test_short_api_windows_do_not_skip_unseen_source_rows(self):
        returned, offsets = self.crawl(response_cap=2)
        self.assertEqual((returned, self.count()), (9, 9))
        self.assertEqual(offsets[:5], [0, 2, 4, 6, 8])

    def test_cancel_mid_page_and_overlap_restart(self):
        with self.assertRaises(CrawlCancelled):
            self.crawl(cancel_at=2)
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.crawl()[0], 7)
        self.assertEqual(self.count(), 9)


for institution in SITES:
    name = institution.replace('-', '_')
    globals()['Hal_'+name] = type('Hal_'+name, (HalOffsetContract, unittest.TestCase), {'SITE': institution+'-hal-science-search'})
    cls = type('HalPostgres_'+name, (HalOffsetContract, unittest.TestCase), {'SITE': institution+'-hal-science-search', 'BACKEND': 'postgres'})
    globals()['HalPostgres_'+name] = unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')(cls)
del cls


if __name__ == '__main__':
    unittest.main()
