"""Real persistence regression tests; only source HTTP is replaced by fixtures."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
from crawler import db, db_libertree
from crawler.base_crawler import CrawlCancelled
from crawler.generic_crawler import GenericCrawler


class CollectionContract:
    def crawler(self, *, pagination=True, single=False):
        config = {
            "site_id": "generic-fixture", "site_name": "Fixture",
            "base_url": "https://fixture.invalid", "crawl_type": "single-page" if single else "html",
            "list_page": {"url": "https://fixture.invalid/list", "selectors": {
                "item_container": "li", "item_link": "a", "link": "a"}},
        }
        if pagination:
            config["list_page"]["pagination"] = {"type": "query_param", "param": "page", "start": 1, "step": 1}
        path = Path(self.tmp.name) / "config.json"
        path.write_text(json.dumps(config))
        c = GenericCrawler(path, self.conn)
        c.delivery_mode = "full"
        return c

    @staticmethod
    def html(ids):
        return BeautifulSoup("<ul>" + "".join(
            f'<li><a href="/doc/{i}">Document {i}</a></li>' for i in ids
        ) + "</ul>", "html.parser")

    def run_pages(self, c, pages, limit=None):
        def fetch(url, params=None):
            return self.html(pages.get((params or {}).get("page", 1), []))
        with patch.object(c, "_fetch_page", side_effect=fetch) as network:
            saved = c.crawl(limit)
        return saved, network

    def count(self):
        row = self.conn.execute("SELECT count(*) AS n FROM documents").fetchone()
        return row["n"] if isinstance(row, dict) else row[0]

    def test_new_limit_survives_repeated_runs_and_known_pages(self):
        pages = {1: [1, 2], 2: [3, 4], 3: [5, 6], 4: [7]}
        for expected, new in ((3, 3), (6, 3), (7, 1), (7, 0)):
            c = self.crawler()
            actual, _ = self.run_pages(c, pages, limit=3)
            self.assertEqual(actual, new)
            self.assertEqual(self.count(), expected)
            self.assertFalse(c.delivery_exhausted)

    def test_incremental_arbitrary_order_reaches_new_after_two_known_pages(self):
        self.run_pages(self.crawler(), {1: [1, 2], 2: [3, 4]})
        c = self.crawler()
        c.delivery_mode = "incremental"
        saved, _ = self.run_pages(c, {1: [1, 2], 2: [3, 4], 3: [5]}, limit=1)
        self.assertEqual((saved, self.count()), (1, 5))

    def test_no_pagination_fetches_once_and_never_claims_exhaustion(self):
        for expected in (2, 0):
            c = self.crawler(pagination=False)
            saved, network = self.run_pages(c, {1: [1, 2]}, limit=3)
            self.assertEqual(saved, expected)
            self.assertEqual(network.call_count, 1)
            self.assertEqual(c.delivery_listing_stop, "pagination_unavailable")
            self.assertEqual(c.delivery_source_failure, "configuration")
            self.assertFalse(c.delivery_exhausted)
        self.assertEqual(self.count(), 2)

    def test_ignored_page_parameter_and_cycle_stop_before_repeat_saves(self):
        for pages, calls in (({1: [1, 2], 2: [2, 1]}, 2),
                             ({1: [1, 2], 2: [3], 3: [2, 1]}, 3)):
            c = self.crawler()
            _, network = self.run_pages(c, pages)
            self.assertEqual(network.call_count, calls)
            self.assertEqual(c.delivery_listing_stop, "repeated_listing")
            self.assertEqual(c.delivery_source_failure, "invalid_response")
            self.assertFalse(c.delivery_exhausted)

    def test_overlap_between_distinct_pages_is_not_a_cycle(self):
        c = self.crawler()
        saved, _ = self.run_pages(c, {1: [1, 2], 2: [2, 3], 3: [3, 4]})
        self.assertEqual((saved, self.count()), (4, 4))
        self.assertIsNone(c.delivery_source_failure)

    def test_invalid_pagination_never_requests(self):
        for key, value in (("step", 0), ("step", -1), ("step", "1"), ("start", True), ("param", None)):
            c = self.crawler()
            c._config["list_page"]["pagination"][key] = value
            saved, network = self.run_pages(c, {1: [1]})
            self.assertEqual((saved, network.call_count), (0, 0))
            self.assertEqual(c.delivery_source_failure, "configuration")

    def test_fetch_failure_preserves_saved_rows_and_is_not_exhaustion(self):
        c = self.crawler()
        with patch.object(c, "_fetch_page", side_effect=[self.html([1, 2]), None]):
            self.assertEqual(c.crawl(), 2)
        self.assertEqual(self.count(), 2)
        self.assertEqual(c.delivery_source_failure, "fetch_failed")
        self.assertFalse(c.delivery_exhausted)

    def test_cancel_and_overlap_restart_keep_all_rows(self):
        c = self.crawler()
        c.delivery_should_cancel = lambda: self.count() >= 2
        with self.assertRaises(CrawlCancelled):
            self.run_pages(c, {1: [1, 2, 3], 2: [4]})
        saved, _ = self.run_pages(self.crawler(), {1: [1, 2, 3], 2: [4]})
        self.assertEqual((saved, self.count()), (2, 4))

    def test_single_page_counts_new_only(self):
        for expected in (3, 0):
            c = self.crawler(single=True)
            saved, network = self.run_pages(c, {1: [1, 2, 3]})
            self.assertEqual(saved, expected)
            self.assertEqual(network.call_count, 1)
            self.assertFalse(c.delivery_exhausted)
        self.assertEqual(self.count(), 3)

    def test_missing_config_and_unimplemented_api_are_explicit_failures(self):
        for api in (False, True):
            c = self.crawler()
            del c._config["list_page"]
            if api:
                c._config["crawl_type"] = "api"
            saved, network = self.run_pages(c, {})
            self.assertEqual((saved, network.call_count), (0, 0))
            self.assertEqual(c.delivery_source_failure, "configuration")

    def common_setup(self, backend):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.enterContext(patch.dict(os.environ, {"LIBERTREE_DB_BACKEND": backend}))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))


class SQLiteCollectionTests(CollectionContract, unittest.TestCase):
    def setUp(self):
        self.common_setup("sqlite")
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute("PRAGMA foreign_keys=ON")
        db_libertree.init_db(self.conn)
        db_libertree.upsert_site(self.conn, "generic-fixture", "Fixture", "https://fixture.invalid")


class LegacyCollectionTests(CollectionContract, unittest.TestCase):
    def setUp(self):
        self.common_setup("sqlite")
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute("PRAGMA foreign_keys=ON")
        db.init_db(self.conn)
        self.conn.execute("INSERT INTO sites(id,name,base_url) VALUES(?,?,?)",
                          ("generic-fixture", "Fixture", "https://fixture.invalid"))
        self.conn.commit()


@unittest.skipUnless(os.environ.get("TEST_PG_DSN"), "requires disposable PostgreSQL")
class PostgresCollectionTests(CollectionContract, unittest.TestCase):
    def setUp(self):
        from crawler import db_pg
        self.common_setup("postgres")
        self.conn = db_pg.open_db(os.environ["TEST_PG_DSN"])
        self.addCleanup(self.conn.close)
        self.conn.execute("DROP SCHEMA public CASCADE")
        self.conn.execute("CREATE SCHEMA public")
        self.conn.commit()
        db_pg.init_db(self.conn)
        db_pg.upsert_site(self.conn, "generic-fixture", "Fixture", "https://fixture.invalid")
