import contextlib
import importlib.util
import io
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from crawler import db, db_libertree
from crawler.base_crawler import CrawlCancelled

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('bmi_new_save_limit', ROOT / 'crawler/sites/custom/bmi-gv-at-114.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
HTML = (ROOT / 'tests/fixtures/bmi_new_saves/disclosures.html').read_text()
SID = 'bmi-gv-at-114'


@contextlib.contextmanager
def isolated_crawler(legacy=False):
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    if legacy:
        db.init_db(conn)
        db.register_site(conn, SID, SID, 'https://www.bmi.gv.at')
    else:
        db_libertree.init_db(conn)
        conn.execute('INSERT INTO sites(site_id,site_name,site_url) VALUES(?,?,?)', (SID, SID, 'https://www.bmi.gv.at'))
        conn.commit()
    crawler = MODULE.BmiGvAt114Crawler(conn, delay=0)

    def fetch(url):
        assert url == MODULE._LIST_URL, 'Unexpected network target'
        return HTML

    with patch.dict('os.environ', {'LIBERTREE_DB_BACKEND': 'sqlite'}), patch.object(crawler, '_curl_get', side_effect=fetch), contextlib.redirect_stdout(io.StringIO()):
        try:
            yield crawler, conn
        finally:
            crawler._session.close()
            conn.close()


class BmiNewSaveLimitTests(unittest.TestCase):
    def test_v2_repeat_reports_zero_and_preserves_existing_rows(self):
        with isolated_crawler() as (crawler, conn):
            self.assertEqual(crawler.crawl(), 42)
            before = [tuple(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
            self.assertEqual(len(before), 42)
            self.assertEqual(crawler.crawl(), 0)
            self.assertEqual([tuple(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')], before)

    def test_known_prefix_does_not_consume_new_record_limit(self):
        with isolated_crawler() as (crawler, conn):
            self.assertEqual(crawler.crawl(limit=3), 3)
            self.assertEqual(crawler.crawl(limit=2), 2)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM documents').fetchone()[0], 5)

    def test_cancelled_replay_can_add_new_rows_after_known_prefix(self):
        with isolated_crawler() as (crawler, conn):
            crawler.delivery_should_cancel = lambda: conn.execute('SELECT COUNT(*) FROM documents').fetchone()[0] >= 5
            with self.assertRaises(CrawlCancelled):
                crawler.crawl()
            crawler.delivery_should_cancel = lambda: False
            self.assertEqual(crawler.crawl(limit=2), 2)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM documents').fetchone()[0], 7)

    def test_legacy_reports_actual_inserts_not_successful_upserts(self):
        with isolated_crawler(legacy=True) as (crawler, conn):
            returned = crawler.crawl()
            self.assertEqual(returned, conn.execute('SELECT COUNT(*) FROM documents').fetchone()[0])
            self.assertEqual(crawler.crawl(), 0)


if __name__ == '__main__':
    unittest.main()
