# -*- coding: utf-8 -*-
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler.db_libertree import init_db
from scripts.backfill_site_fields import backfill


def _mkdb():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    return conn


def _ins(conn, site_id, post, url, **kw):
    cols = {"site_id": site_id, "post_number": post, "meta_url": url,
            "title": kw.get("title", "t"),
            "published_date": kw.get("published_date")}
    conn.execute(
        "INSERT INTO documents (site_id, post_number, meta_url, title, published_date)"
        " VALUES (:site_id, :post_number, :meta_url, :title, :published_date)", cols)
    conn.commit()


class TestBackfill(unittest.TestCase):
    def test_date_backfilled_only_when_empty_target_has_value(self):
        main, scratch = _mkdb(), _mkdb()
        _ins(main, "s", "1", "https://x/1", published_date="")
        _ins(scratch, "s", "1", "https://x/1", published_date="2025-03-01")
        rep = backfill(main, scratch, "s", ["published_date"], apply=True)
        self.assertEqual(rep["updated"]["published_date"], 1)
        got = main.execute("SELECT published_date FROM documents").fetchone()[0]
        self.assertEqual(got, "2025-03-01")

    def test_title_only_replaced_when_mojibake(self):
        main, scratch = _mkdb(), _mkdb()
        _ins(main, "s", "1", "https://x/1", title="dann�ggiat�")
        _ins(main, "s", "2", "https://x/2", title="fine title")
        _ins(scratch, "s", "1", "https://x/1", title="danneggiatà")
        _ins(scratch, "s", "2", "https://x/2", title="DIFFERENT")
        rep = backfill(main, scratch, "s", ["title"], apply=True)
        self.assertEqual(rep["updated"]["title"], 1)
        titles = [r[0] for r in main.execute(
            "SELECT title FROM documents ORDER BY seq_id").fetchall()]
        self.assertEqual(titles, ["danneggiatà", "fine title"])

    def test_dry_run_no_write(self):
        main, scratch = _mkdb(), _mkdb()
        _ins(main, "s", "1", "https://x/1", published_date="")
        _ins(scratch, "s", "1", "https://x/1", published_date="2025-03-01")
        backfill(main, scratch, "s", ["published_date"], apply=False)
        self.assertEqual(
            main.execute("SELECT published_date FROM documents").fetchone()[0], "")

    def test_unmatched_counted(self):
        main, scratch = _mkdb(), _mkdb()
        _ins(scratch, "s", "9", "https://x/9", published_date="2025-01-01")
        rep = backfill(main, scratch, "s", ["published_date"], apply=True)
        self.assertEqual(rep["unmatched"], 1)


if __name__ == "__main__":
    unittest.main()
