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


class ScienceOnPageErrorTest(unittest.TestCase):
    """E5001 on a page: re-read it row by row so only the broken row is lost."""

    class _Client(SCIENCEON.ScienceOnClient):
        def __init__(self, total, broken, whole_page_error=True):
            self.total, self.broken = total, set(broken)
            self.unreadable = []
            self.calls = []
            self.delay = 0.0
            self.should_cancel = lambda: None

        def _wait(self, seconds):
            pass

        def search(self, query, *, page=1, row_count=100, sort_field=None):
            self.calls.append((page, row_count))
            first = (page - 1) * row_count + 1
            rows = range(first, min(first + row_count, self.total + 1))
            if self.broken & set(rows):
                raise SCIENCEON.ScienceOnError("rejected", code="E5001")
            return self.total, [{"CN": "JAKO%04d" % n} for n in rows]

    def test_only_the_broken_row_is_lost(self):
        client = self._Client(total=250, broken={153})
        got = [r["CN"] for r in client.iter_search({"CN": "JAKO*"})]
        self.assertEqual(len(got), 249)
        self.assertNotIn("JAKO0153", got)
        self.assertEqual(len(set(got)), 249)
        self.assertEqual(len(client.unreadable), 1)
        self.assertEqual(client.unreadable[0]["position"], 153)

    def test_other_errors_are_not_swallowed(self):
        client = self._Client(total=250, broken=set())

        def boom(*args, **kwargs):
            raise SCIENCEON.ScienceOnError("rejected", code="E9999")

        client.search = boom
        with self.assertRaises(SCIENCEON.ScienceOnError):
            list(client.iter_search({"CN": "JAKO*"}))

    def test_one_broken_row_costs_few_calls(self):
        client = self._Client(total=250, broken={153})
        list(client.iter_search({"CN": "JAKO*"}))
        self.assertLessEqual(len(client.calls), 25)  # was 100 single-row calls before
        self.assertEqual(len(client.unreadable), 1)

    def test_last_partial_page_with_broken_row(self):
        client = self._Client(total=137, broken={137})
        got = [r["CN"] for r in client.iter_search({"CN": "JAKO*"})]
        self.assertEqual(len(got), 136)
        self.assertEqual(len(set(got)), 136)
        self.assertEqual([u["position"] for u in client.unreadable], [137])

    def test_two_broken_rows_on_one_page(self):
        client = self._Client(total=120, broken={5, 77})
        got = [r["CN"] for r in client.iter_search({"CN": "JAKO*"})]
        self.assertEqual(len(got), 118)
        self.assertEqual(len(client.unreadable), 2)

    def test_slice_with_one_unreadable_row_still_passes_completeness(self):
        client = self._Client(total=200, broken={150})
        crawler = SCIENCEON.ScienceOnDomesticCrawler(None, client=client)
        crawler.SORT_FIELDS = ("title",)
        captured = []

        def save(document):
            captured.append(document)
            crawler._last_save_created = True
            return len(captured)

        crawler._save_paper = save
        seen, saved = crawler._collect_terminal("JAKO", "JAKO", 200)
        self.assertEqual((seen, saved), (199, 199))
        self.assertEqual(client.unreadable, [])  # reported then cleared

    def _collect(self, client, total):
        crawler = SCIENCEON.ScienceOnDomesticCrawler(None, client=client)
        crawler.SORT_FIELDS = ("title",)
        crawler._save_paper = lambda document: setattr(crawler, "_last_save_created", True) or 1
        return crawler._collect_terminal("JAKO", "JAKO", total)

    def test_small_slice_with_one_unreadable_row_passes(self):
        client = self._Client(total=85, broken={40})
        self.assertEqual(self._collect(client, 85), (84, 84))

    def test_later_sort_that_silently_loses_rows_does_not_hide_the_complete_pass(self):
        client = self._Client(total=85, broken={40})
        real = client.search

        def search(query, *, page=1, row_count=100, sort_field=None):
            total, rows = real(query, page=page, row_count=row_count, sort_field=sort_field)
            if sort_field == "jtitle":  # tie-heavy sort: drops rows without any error
                rows = [r for r in rows if int(r["CN"][4:]) % 10]
            return total, rows

        client.search = search
        crawler = SCIENCEON.ScienceOnDomesticCrawler(None, client=client)
        crawler._save_paper = lambda document: setattr(crawler, "_last_save_created", True) or 1
        self.assertEqual(crawler._collect_terminal("JAKO", "JAKO", 85), (84, 84))

    def test_small_slice_missing_a_row_without_e5001_still_fails(self):
        client = self._Client(total=85, broken=set())
        real = client.search

        def short(query, *, page=1, row_count=100, sort_field=None):
            total, rows = real(query, page=page, row_count=row_count, sort_field=sort_field)
            return total, [r for r in rows if r["CN"] != "JAKO0040"]

        client.search = short
        with self.assertRaises(SCIENCEON.ScienceOnError):
            self._collect(client, 85)


class ScienceOnPassCountTest(unittest.TestCase):
    """One sort pass is enough when it reaches the reported total."""

    class _Client:
        def __init__(self, total, per_sort):
            self.total, self.per_sort = total, per_sort
            self.unreadable = []
            self.sorts_read = []

        def search(self, query, **kwargs):
            return self.total, []

        def iter_search(self, query, *, sort_field=None):
            self.sorts_read.append(sort_field)
            for n in self.per_sort[sort_field]:
                yield {"CN": "JAKO%04d" % n, "DBCode": "JAKO", "Title": "t%d" % n}

    def _crawler(self, client):
        crawler = SCIENCEON.ScienceOnDomesticCrawler(None, client=client)
        saved = []

        def save(document):
            saved.append(document)
            crawler._last_save_created = True
            return len(saved)

        crawler._save_paper = save
        return crawler, saved

    def test_default_order_starts_with_title(self):
        self.assertEqual(SCIENCEON.ScienceOnDomesticCrawler.SORT_FIELDS[0], "title")

    def test_stops_after_first_pass_that_reaches_total(self):
        client = self._Client(5, {"title": range(5), "pubyear": range(5)})
        crawler, saved = self._crawler(client)
        crawler.SORT_FIELDS = ("title", "pubyear")
        self.assertEqual(crawler._collect_terminal("JAKO", "JAKO", 5), (5, 5))
        self.assertEqual(client.sorts_read, ["title"])

    def test_short_first_pass_is_completed_by_the_next_sort(self):
        client = self._Client(5, {"title": [0, 1, 2], "pubyear": [2, 3, 4]})
        crawler, saved = self._crawler(client)
        crawler.SORT_FIELDS = ("title", "pubyear")
        self.assertEqual(crawler._collect_terminal("JAKO", "JAKO", 5), (5, 5))
        self.assertEqual(client.sorts_read, ["title", "pubyear"])
        self.assertEqual(len(saved), 5)
