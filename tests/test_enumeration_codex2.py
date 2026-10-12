"""Cross-review (Codex) batch 2: H10, H12, M01, M02, M03."""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from crawler import db_libertree  # noqa: E402
from crawler.enumeration.adapters import Adapter, AdapterConfigError  # noqa: E402
from crawler.enumeration.files import FilePass  # noqa: E402
from crawler.enumeration.http import Response  # noqa: E402
from crawler.enumeration.report import site_report  # noqa: E402
from crawler.enumeration.urls import build_url  # noqa: E402
from tests.enumeration_fakes import Env, FakeBoard, FakeTransport, board_plan  # noqa: E402
from tests.test_enumeration_ledgers import FakeCustomCrawler, custom_plan  # noqa: E402


class SavePathFailureTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_transient_database_error_is_retried_not_permanent(self):
        crawler = FakeCustomCrawler(self.env.conn)
        original = crawler._save_paper
        calls = {"n": 0}

        def flaky(paper):
            calls["n"] += 1
            if calls["n"] == 2:
                raise sqlite3.OperationalError("database is locked")
            return original(paper)
        crawler._save_paper = flaky
        first = self.env.run(custom_plan(), FakeBoard(6), crawler=crawler)
        self.assertEqual(first["status"], "complete", first)
        row = self.env.store._all("SELECT item_key, status, reason FROM enum_item WHERE reason LIKE 'save_%'")
        self.assertEqual([(r["status"], r["reason"]) for r in row], [("failed_transient", "save_transient:OperationalError")])
        self.env.run(custom_plan(), FakeBoard(6), crawler=FakeCustomCrawler(self.env.conn))  # retry pass
        self.assertEqual(len(self.env.docs()), 5)  # all but the parse-error item 5

    def test_missing_adapter_method_is_a_site_config_error(self):
        site = custom_plan(detail_method="_no_such_method")
        result = self.env.run(site, FakeBoard(6), crawler=FakeCustomCrawler(self.env.conn))
        self.assertEqual(result["status"], "config_error", result)
        self.assertEqual(self.env.store._all("SELECT COUNT(*) AS n FROM enum_item")[0]["n"], 0)

    def test_bad_argument_token_is_a_site_config_error(self):
        site = custom_plan(detail_args=["html", "no_such_token"])
        with self.assertRaises(AdapterConfigError):
            Adapter("fake-site", site["adapter"], self.env.conn, crawler=FakeCustomCrawler(self.env.conn))

    def test_failed_items_can_be_reprocessed_by_reason(self):
        crawler = FakeCustomCrawler(self.env.conn)
        self.env.run(custom_plan(), FakeBoard(6), crawler=crawler)
        self.assertEqual(self.env.store.item(self.env.site_id, "5")["reason"], "parse_error:ValueError")
        saved_before = {d["seq_id"] for d in self.env.docs()}

        class Fixed(FakeCustomCrawler):
            def _parse_detail(self, raw, item):
                item = dict(item)
                if item["post_number"] == "5":
                    item["post_number"] = "5"
                    return {"external_id": "5", "title": "Title 5", "url": "https://board.test/view?id=5",
                            "authors": "[]", "keywords": "[]", "metadata": "{}"}
                return super()._parse_detail(raw, item)
        count = self.env.store.requeue_failed(self.env.site_id, ["parse_error:"])
        self.assertEqual(count, 1)
        result = self.env.run(custom_plan(), FakeBoard(6), crawler=Fixed(self.env.conn), retry_only=True)
        self.assertEqual(result["stats"]["retried"], 1)
        self.assertEqual(self.env.store.item(self.env.site_id, "5")["status"], "saved")
        self.assertTrue(saved_before < {d["seq_id"] for d in self.env.docs()})
        self.assertEqual(len(self.env.docs()), 6)


class RepeatedQueryKeyTest(unittest.TestCase):
    def test_repeated_keys_survive_and_only_overridden_keys_change(self):
        self.assertEqual(build_url("https://s.test/list?f=a&f=b", {"page": 1}), "https://s.test/list?f=a&f=b&page=1")
        self.assertEqual(build_url("https://s.test/l?fq=type:report&fq=language:en&page=3", {"page": 4}),
                         "https://s.test/l?fq=type%3Areport&fq=language%3Aen&page=4")
        self.assertEqual(build_url("https://s.test/l?f=a&f=b&q=", {"f": ["c", "d"]}), "https://s.test/l?f=c&f=d&q=")
        self.assertEqual(build_url("https://s.test/l?q=&x=1", {}), "https://s.test/l?q=&x=1")


class FilePassFailureTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.env.run(board_plan(), FakeBoard(3))
        self.plan = self.env.plan(board_plan())

    def tearDown(self):
        self.env.close()

    def file_pass(self, transport, **kw):
        return FilePass(self.env.store, self.env.fetcher(self.plan, transport), blob_root=self.env.root / "blobs",
                        doc_conn=self.env.conn, max_attempts=2, **kw)

    def test_storage_error_is_ledgered_with_bounded_attempts(self):
        t = FakeTransport(FakeBoard(3))
        with mock.patch("crawler.blob_storage.save_pdf", side_effect=OSError("disk full")):
            stats = self.file_pass(t).run(self.env.site_id)
            self.assertEqual(stats["failed_transient"], 3)
            stats = self.file_pass(t).run(self.env.site_id)
            self.assertEqual(stats["failed"], 3)
            stats = self.file_pass(t).run(self.env.site_id)
        self.assertEqual(stats["done"] + stats["failed"] + stats["failed_transient"], 0)  # bounded
        reasons = {f["reason"] for f in self.env.store.files(self.env.site_id)}
        self.assertEqual(reasons, {"retries_exhausted:storage_error:OSError"})

    def test_crash_after_blob_save_is_reconciled_without_download(self):
        original = self.env.store.update_file
        calls = {"n": 0}

        def crash(*args, **kwargs):
            if kwargs.get("status") == "done":
                calls["n"] += 1
                if calls["n"] == 1:
                    raise KeyboardInterrupt("killed after the blob and DB update")
            return original(*args, **kwargs)
        self.env.store.update_file = crash
        with self.assertRaises(KeyboardInterrupt):
            self.file_pass(FakeTransport(FakeBoard(3))).run(self.env.site_id)
        self.env.store.update_file = original
        t = FakeTransport(FakeBoard(3))
        stats = self.file_pass(t).run(self.env.site_id)
        self.assertEqual(stats["done"], 3)
        self.assertEqual(stats.get("reconciled"), 1)
        self.assertEqual(sum("/files/" in u for u in t.urls), 2)  # the reconciled file is not fetched again


class BackendConsistencyTest(unittest.TestCase):
    def test_sqlite_connection_with_postgres_backend_env_is_refused(self):
        env = Env()
        try:
            with mock.patch.dict(os.environ, {"LIBERTREE_DB_BACKEND": "postgres"}):
                with self.assertRaises(AdapterConfigError) as ctx:
                    Adapter("fake-site", {"type": "list_only"}, env.conn)
            self.assertIn("backend_mismatch", str(ctx.exception))
        finally:
            env.close()


class ReportScopeTest(unittest.TestCase):
    def test_current_generation_and_all_time_are_separate(self):
        env = Env()
        try:
            env.run(board_plan(), FakeBoard(25))
            narrowed = board_plan(max_pages=1)  # a different plan: a new generation
            narrowed["listing"]["params"] = {"scope": "recent"}
            env.run(narrowed, FakeBoard(25), refresh=True)
            report = site_report(env.store, env.site_id, doc_conn=env.conn)
            self.assertEqual(report["saved_all_time"], 25)
            self.assertEqual(report["saved_current_generation"], report["listed_unique"])
            self.assertLess(report["saved_current_generation"], 25)
        finally:
            env.close()

    def test_date_fill_counts_distinct_documents(self):
        env = Env()
        try:
            db_libertree.insert_document(env.conn, {"site_id": "fake-site", "post_number": "3",
                                                    "meta_url": "https://board.test/view?id=3", "title": "Title 3",
                                                    "published_date": "2000-01-04"})
            env.run(board_plan(), FakeBoard(5))
            # Another ledger key pointing at an already saved document.
            env.store.record_item(env.site_id, "alias-3", "saved", seq_id=1, reason="existing_document")
            report = site_report(env.store, env.site_id, doc_conn=env.conn)
            self.assertEqual(report["date_fill"]["documents"], 5)
        finally:
            env.close()


if __name__ == "__main__":
    unittest.main()
