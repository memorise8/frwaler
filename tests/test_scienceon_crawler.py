import importlib.util
import os
from pathlib import Path
import unittest
from unittest import mock


CRAWLER_PATH = (
    Path(__file__).resolve().parent.parent
    / "crawler"
    / "sites"
    / "custom"
    / "scienceon-api.py"
)
SPEC = importlib.util.spec_from_file_location("scienceon_test_module", CRAWLER_PATH)
SCIENCEON = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCIENCEON)
TEST_PG_DSN = os.environ.get("TEST_PG_DSN")


class _BackfillClient:
    def search(self, query, **kwargs):
        prefix = query["CN"][:-1]
        total = {"JAKO": 3, "JAKO0": 2, "JAKO1": 1}.get(prefix, 0)
        return total, []

    def iter_search(self, query, **kwargs):
        prefix = query["CN"][:-1]
        rows = {
            "JAKO0": [
                {"CN": "JAKO0A", "DBCode": "JAKO", "Title": "Alpha"},
                {"CN": "JAKO0B", "DBCode": "JAKO", "Title": "Beta"},
            ],
            "JAKO1": [
                {"CN": "JAKO1A", "DBCode": "JAKO", "Title": "Gamma"}
            ],
        }
        yield from rows.get(prefix, [])


class ScienceOnCrawlerUnitTest(unittest.TestCase):
    def test_registry_contract_uses_two_string_site_ids(self):
        from crawler.sites import CRAWLERS

        self.assertEqual(CRAWLERS["scienceon-api"].site_id, "scienceon-api")
        self.assertEqual(
            CRAWLERS["scienceon-foreign-api"].site_id, "scienceon-foreign-api"
        )
        self.assertEqual(SCIENCEON.ScienceOnDomesticCrawler.__abstractmethods__, frozenset())

    @staticmethod
    def _capture(crawler):
        captured = []

        def save(document):
            captured.append(document)
            crawler._last_save_created = True
            return len(captured)

        crawler._save_paper = save
        return captured

    def test_domestic_and_foreign_mixed_prefixes_are_separated(self):
        domestic = SCIENCEON.ScienceOnDomesticCrawler(None, client=object())
        foreign = SCIENCEON.ScienceOnForeignCrawler(None, client=object())
        domestic_rows = self._capture(domestic)
        foreign_rows = self._capture(foreign)
        cfko = {"CN": "NPAP1", "DBCode": "CFKO", "Title": "Domestic"}
        cffo = {"CN": "NPAP2", "DBCode": "CFFO", "Title": "Foreign"}

        self.assertTrue(domestic._save_record("NPAP", cfko))
        self.assertFalse(domestic._save_record("NPAP", cffo))
        self.assertFalse(foreign._save_record("NPAP", cfko))
        self.assertTrue(foreign._save_record("NPAP", cffo))
        self.assertEqual(domestic_rows[0]["external_id"], "NPAP1")
        self.assertEqual(foreign_rows[0]["external_id"], "NPAP2")

    def test_foreign_mixed_prefix_rejects_unknown_dbcode(self):
        crawler = SCIENCEON.ScienceOnForeignCrawler(None, client=object())
        captured = self._capture(crawler)
        self.assertFalse(
            crawler._save_record("ATN", {"CN": "ATN1", "Title": "Unknown"})
        )
        self.assertEqual(captured, [])

    def test_backfill_cursor_resumes_at_next_complete_terminal(self):
        crawler = SCIENCEON.ScienceOnDomesticCrawler(None, client=_BackfillClient())
        crawler.ROOTS = ("JAKO",)
        crawler.MAX_SLICE = 2
        crawler.SORT_FIELDS = (None,)
        crawler.delivery_mode = "backfill"
        self._capture(crawler)
        with mock.patch.dict(os.environ, {"SCIENCEON_SLICE_BUDGET": "1"}):
            crawler.crawl()
        self.assertEqual(
            crawler.delivery_pending_cursor,
            {"root_index": 0, "prefix": "JAKO1"},
        )
        self.assertEqual(crawler.delivery_cursor_items_done, 2)
        self.assertFalse(crawler.delivery_exhausted)

    def test_incomplete_slice_fails_without_cursor_advance(self):
        class IncompleteClient:
            def search(self, query, **kwargs):
                return 2, []

            def iter_search(self, query, **kwargs):
                yield {"CN": "JAKO1", "DBCode": "JAKO", "Title": "Only one"}

        crawler = SCIENCEON.ScienceOnDomesticCrawler(None, client=IncompleteClient())
        crawler.ROOTS = ("JAKO",)
        crawler.MAX_SLICE = 2
        crawler.SORT_FIELDS = (None,)
        crawler.delivery_mode = "backfill"
        self._capture(crawler)
        with self.assertRaisesRegex(SCIENCEON.ScienceOnError, "incomplete"):
            crawler.crawl()
        self.assertIsNone(crawler.delivery_pending_cursor)
        self.assertFalse(crawler.delivery_exhausted)

    def test_full_mode_is_rejected_in_favour_of_backfill(self):
        crawler = SCIENCEON.ScienceOnDomesticCrawler(None, client=object())
        crawler.delivery_mode = "full"
        with self.assertRaisesRegex(ValueError, "backfill"):
            crawler.crawl()


@unittest.skipUnless(TEST_PG_DSN, "set TEST_PG_DSN to run")
class ScienceOnWorkerIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from crawler import db_pg
        from delivery.be.migrate import migrate

        conn = db_pg.open_db(TEST_PG_DSN)
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
        conn.close()
        migrate(TEST_PG_DSN)

    def setUp(self):
        from crawler import db_pg

        self.conn = db_pg.open_db(TEST_PG_DSN)
        db_pg.upsert_site(
            self.conn,
            "scienceon-fixture",
            "ScienceON Fixture",
            "https://apigateway.kisti.re.kr",
        )
        self.conn.execute("DELETE FROM crawl_jobs WHERE site_id='scienceon-fixture'")
        self.conn.execute("DELETE FROM crawl_site_progress WHERE site_id='scienceon-fixture'")
        self.conn.execute("DELETE FROM documents WHERE site_id='scienceon-fixture'")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_migration_seeds_both_scienceon_sites(self):
        rows = self.conn.execute(
            "SELECT site_id FROM sites WHERE site_id LIKE 'scienceon%' ORDER BY site_id"
        ).fetchall()
        self.assertEqual(
            [row["site_id"] for row in rows],
            ["scienceon-api", "scienceon-fixture", "scienceon-foreign-api"],
        )

    def test_worker_backfill_persists_cursor_and_requeues(self):
        from delivery.worker import jobs, worker

        class FixtureCrawler(SCIENCEON.ScienceOnDomesticCrawler):
            site_id = "scienceon-fixture"
            ROOTS = ("JAKO",)
            MAX_SLICE = 2
            SORT_FIELDS = (None,)

            def __init__(self, db_conn, delay=0):
                super().__init__(db_conn, delay=delay, client=_BackfillClient())

        jobs.enqueue_job(
            self.conn,
            "scienceon-fixture",
            mode="backfill",
            requested_by="test",
        )
        claimed = jobs.claim_next_job(self.conn, worker_id="scienceon-test")
        with mock.patch.dict(os.environ, {"SCIENCEON_SLICE_BUDGET": "1"}):
            saved = worker.run_job(
                self.conn,
                claimed,
                crawler_registry={"scienceon-fixture": FixtureCrawler},
                delay=0,
            )

        self.assertEqual(saved, 2)
        progress = self.conn.execute(
            "SELECT cursor FROM crawl_site_progress WHERE site_id='scienceon-fixture'"
        ).fetchone()
        self.assertEqual(progress["cursor"], {"root_index": 0, "prefix": "JAKO1"})
        queued = self.conn.execute(
            "SELECT count(*) AS n FROM crawl_jobs "
            "WHERE site_id='scienceon-fixture' AND status='queued'"
        ).fetchone()["n"]
        self.assertEqual(queued, 1)


if __name__ == "__main__":
    unittest.main()
