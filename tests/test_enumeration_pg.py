"""Save-path safety on SQLite and PostgreSQL (H2): NUL text, int ids, failed save
followed by more saves, duplicate post_number.

The PostgreSQL case needs TEST_PG_ADMIN_DSN pointing at a disposable test server.
It creates a fresh database named enum_test_<timestamp>, runs the scenario and
drops only that database. It never touches any other database.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from crawler import db_libertree  # noqa: E402
from crawler.base_crawler import BaseCrawler  # noqa: E402
from crawler.enumeration.adapters import Adapter  # noqa: E402
from crawler.enumeration.plan import site_plan  # noqa: E402
from crawler.enumeration.runner import SiteRunner  # noqa: E402
from crawler.enumeration.store import Store  # noqa: E402
from crawler.enumeration.http import Fetcher  # noqa: E402
from tests.enumeration_fakes import FakeBoard, FakeTransport, board_plan  # noqa: E402

SITE = "fake-site"


class PaperCrawler(BaseCrawler):
    site_id = SITE
    site_name = "papers"
    base_url = "https://board.test"

    def crawl(self, limit=None):
        return 0

    def _parse_list_page(self, raw):
        return [{"detail_url": f"/view?id={m}", "post_number": m} for m in re.findall(r'href="/view\?id=(\d+)"', raw)]

    def _paper(self, item):
        i = int(item["post_number"])
        paper = {"external_id": str(i), "title": f"Title {i}", "url": f"https://board.test/view?id={i}",
                 "abstract": "", "authors": "[]", "keywords": "[]", "metadata": "{}"}
        if i == 1:
            paper["title"] = "Bad\x00Title"
            paper["abstract"] = "nul\x00inside"
        if i == 2:
            paper["external_id"] = 7  # int: must be stored as text '7'
        if i == 5:
            paper["external_id"] = "7"  # same post_number as item 2, different document: collision
        return paper

    def _save_paper(self, paper):
        if paper["url"].endswith("id=3"):
            try:
                self._conn.execute("SELECT 1/0").fetchone()  # PostgreSQL: error, transaction aborted
            except Exception:
                pass
            raise RuntimeError("controlled save failure")
        return super()._save_paper(paper)


def scenario(test, conn, store_path, gates):
    plan_raw = board_plan(listing={"parser": {"type": "adapter", "key_field": "post_number"}})
    plan_raw["adapter"] = {"type": "crawler", "list_method": "_parse_list_page", "list_args": ["html"],
                           "paper_method": "_paper"}
    plan = site_plan({"sites": {SITE: plan_raw}}, SITE, min_interval=0)
    store = Store(store_path)
    crawler = PaperCrawler(conn)
    adapter = Adapter(SITE, plan.raw["adapter"], conn, crawler=crawler)
    fetcher = Fetcher(SITE, FakeTransport(FakeBoard(6)), gates, interval=0, respect_robots=False,
                      sleep=lambda s: None)
    result = SiteRunner(plan, store, fetcher, adapter, today="2026-10-12").run()
    # Item 5 is an identity collision: enumeration finished, but a person must review it.
    test.assertEqual((result["status"], result["stop_reason"]), ("complete_with_review", "identity_collisions:1"),
                     result)
    rows = [tuple(r) if not hasattr(r, "keys") else (r["post_number"], r["title"], r["abstract"])
            for r in conn.execute("SELECT post_number, title, abstract FROM documents ORDER BY seq_id").fetchall()]
    test.assertEqual([r[0] for r in rows], ["1", "7", "4", "6"])
    test.assertEqual(rows[0][1:], ("BadTitle", "nulinside"))
    ledger = {r["item_key"]: (r["status"], r["reason"]) for r in
              store._all("SELECT item_key, status, reason FROM enum_item WHERE site_id=?", (SITE,))}
    test.assertEqual(ledger["3"], ("failed_permanent", "save_error:RuntimeError"))
    test.assertEqual(ledger["5"], ("failed_permanent", "identity_collision"))
    test.assertEqual(result["duplicates"]["new"], 0)
    store.close()


class SqliteSavePathTest(unittest.TestCase):
    def test_scenario_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db_libertree.open_db(Path(tmp) / "enum_sqlite.db")
            db_libertree.init_db(conn)
            db_libertree.upsert_site(conn, SITE, SITE, "https://board.test")
            scenario(self, conn, Path(tmp) / "state.sqlite", Path(tmp) / "gates")
            conn.close()


@unittest.skipUnless(os.environ.get("TEST_PG_ADMIN_DSN"), "TEST_PG_ADMIN_DSN not set (disposable PG server)")
class PostgresSavePathTest(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict, make_conninfo
        admin = os.environ["TEST_PG_ADMIN_DSN"]
        self.dbname = f"enum_test_{int(time.time() * 1000)}"
        self.admin = psycopg.connect(admin, autocommit=True)
        self.admin.execute(f'CREATE DATABASE "{self.dbname}"')
        params = conninfo_to_dict(admin)
        params["dbname"] = self.dbname
        self.dsn = make_conninfo(**params)
        self.previous = os.environ.get("LIBERTREE_DB_BACKEND")
        os.environ["LIBERTREE_DB_BACKEND"] = "postgres"

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("LIBERTREE_DB_BACKEND", None)
        else:
            os.environ["LIBERTREE_DB_BACKEND"] = self.previous
        self.admin.execute(f'DROP DATABASE IF EXISTS "{self.dbname}" WITH (FORCE)')
        self.admin.close()

    def test_scenario_postgres(self):
        from crawler import db_pg
        conn = db_pg.open_db(self.dsn)
        try:
            db_pg.init_db(conn)
            db_pg.upsert_site(conn, SITE, SITE, "https://board.test")
            with tempfile.TemporaryDirectory() as tmp:
                scenario(self, conn, Path(tmp) / "state.sqlite", Path(tmp) / "gates")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
