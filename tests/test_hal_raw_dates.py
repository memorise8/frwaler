"""HAL source date text must survive the actual adapter and database path."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from crawler import db_libertree
from crawler.base_crawler import BaseCrawler

ROOT = Path(__file__).resolve().parents[1]
SITES = ('enscp', 'enssib', 'icn', 'icp', 'ineris', 'ird', 'polytechnique')
CASES = (
    ('2025', None, 'year', 'partial'),
    ('2025-07', None, 'month', 'partial'),
    ('2025-02-30', None, 'day', 'invalid'),
    ('2025-07-11T12:34:56Z', '2025-07-11', 'day', 'normalized'),
    ('2025-07-11 wrong suffix', None, None, 'unrecognized'),
    ('03/04/2025', None, 'day', 'ambiguous'),
    ('2025-07-11', '2025-07-11', 'day', 'normalized'),
)


class RawDateContract:
    BACKEND = 'sqlite'

    def test_source_dates_survive_native_save_and_repeat(self):
        site = self.SITE+'-hal-science-search'
        spec = importlib.util.spec_from_file_location('raw_dates_'+self.SITE, ROOT/'crawler/sites/custom'/f'{site}.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        cls = next(v for v in vars(module).values() if isinstance(v, type) and issubclass(v, BaseCrawler) and getattr(v, 'site_id', None) == site)
        backend = db_libertree
        if self.BACKEND == 'postgres':
            from crawler import db_pg
            backend = db_pg
            conn = backend.open_db(os.environ['TEST_PG_DSN'])
            conn.execute('DROP SCHEMA public CASCADE'); conn.execute('CREATE SCHEMA public'); conn.commit()
        else:
            conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close); backend.init_db(conn); backend.upsert_site(conn, site, site, 'https://hal.science')
        docs = []
        for n, (raw, _, _, _) in enumerate(CASES, 1):
            docs.append({'docid': n, 'halId_s': f'hal-{n}', 'uri_s': f'https://hal.science/hal-{n}',
                         'title_s': [f'Research {n}'], 'abstract_s': ['Scientific abstract. '*10],
                         'producedDate_s': raw, 'submittedDate_s': raw, 'submittedDate_tdate': raw})
        with patch.dict(os.environ, {'LIBERTREE_DB_BACKEND': self.BACKEND}), patch.object(module.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()):
            for expected in (len(docs), 0):
                c = cls(conn); c.delivery_mode = 'full'
                try:
                    with patch.object(c, '_fetch_page', side_effect=lambda start, rows: {'response': {'numFound': len(docs), 'docs': docs[start:start+rows]}}):
                        self.assertEqual(c.crawl(), expected)
                finally:
                    c._session.close()
        rows = conn.execute('SELECT published_date,published_date_raw,published_date_precision,published_date_status,listed_date,listed_date_raw,listed_date_precision,listed_date_status FROM documents ORDER BY seq_id').fetchall()
        self.assertEqual(len(rows), len(CASES))
        for row, (raw, value, precision, status) in zip(rows, CASES):
            with self.subTest(raw=raw):
                values = tuple(row.values()) if isinstance(row, dict) else tuple(row)
                self.assertEqual(values, (value, raw, precision, status)*2)


for site in SITES:
    globals()['SQLite_'+site] = type('SQLite_'+site, (RawDateContract, unittest.TestCase), {'SITE': site})
    pg = type('Postgres_'+site, (RawDateContract, unittest.TestCase), {'SITE': site, 'BACKEND': 'postgres'})
    globals()['Postgres_'+site] = unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')(pg)
del pg
