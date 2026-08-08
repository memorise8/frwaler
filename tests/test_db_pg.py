# -*- coding: utf-8 -*-
"""crawler.db_pg — Postgres 포트 단위 테스트 (TEST_PG_DSN 필요)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")


@unittest.skipUnless(TEST_PG_DSN, "set TEST_PG_DSN (delivery/scripts/test_pg.sh up) to run")
class DbPgTest(unittest.TestCase):
    def setUp(self):
        from crawler import db_pg
        self.db_pg = db_pg
        self.conn = db_pg.open_db(TEST_PG_DSN)
        self.conn.execute("DROP TABLE IF EXISTS documents CASCADE")
        self.conn.execute("DROP TABLE IF EXISTS sites CASCADE")
        self.conn.commit()
        db_pg.init_db(self.conn)
        db_pg.upsert_site(self.conn, "s1", "Site One", "https://s1.example")

    def tearDown(self):
        self.conn.close()

    def _doc(self, **over):
        d = {"site_id": "s1", "post_number": "100", "meta_url": "https://s1/a",
             "title": "Doc A"}
        d.update(over)
        return d

    def test_insert_returns_seq_id(self):
        seq = self.db_pg.insert_document(self.conn, self._doc())
        self.assertIsInstance(seq, int)
        self.assertGreaterEqual(seq, 1)

    def test_dedup_returns_same_seq_id(self):
        a = self.db_pg.insert_document(self.conn, self._doc())
        b = self.db_pg.insert_document(self.conn, self._doc())
        self.assertEqual(a, b)

    def test_distinct_meta_url_new_row(self):
        a = self.db_pg.insert_document(self.conn, self._doc())
        b = self.db_pg.insert_document(self.conn, self._doc(meta_url="https://s1/b"))
        self.assertNotEqual(a, b)

    def test_find_by_dedup_key(self):
        seq = self.db_pg.insert_document(self.conn, self._doc())
        found = self.db_pg.find_by_dedup_key(self.conn, "s1", "100", "https://s1/a")
        self.assertEqual(found, seq)
        self.assertIsNone(self.db_pg.find_by_dedup_key(self.conn, "s1", "999", "https://s1/z"))

    def test_get_max_post_number(self):
        self.db_pg.insert_document(self.conn, self._doc(post_number="100", meta_url="https://s1/a"))
        self.db_pg.insert_document(self.conn, self._doc(post_number="205", meta_url="https://s1/b"))
        self.assertEqual(self.db_pg.get_max_post_number(self.conn, "s1"), "205")

    def test_get_document_roundtrip(self):
        seq = self.db_pg.insert_document(self.conn, self._doc(title="Hello"))
        row = self.db_pg.get_document(self.conn, seq)
        self.assertEqual(row["title"], "Hello")
        self.assertEqual(row["site_id"], "s1")

    def test_null_post_number_not_deduped_by_unique(self):
        a = self.db_pg.insert_document(self.conn, self._doc(post_number=None, meta_url="https://s1/n1"))
        b = self.db_pg.insert_document(self.conn, self._doc(post_number=None, meta_url="https://s1/n1"))
        # find_by_dedup_key(None) 은 기존 행을 찾아 같은 seq 반환해야 함(동작 보존)
        self.assertEqual(a, b)

    def test_update_document_pdf(self):
        seq = self.db_pg.insert_document(self.conn, self._doc())
        self.db_pg.update_document_pdf(self.conn, seq, downloaded=True, size_bytes=123, sha256="abc")
        row = self.db_pg.get_document(self.conn, seq)
        self.assertEqual(row["pdf_downloaded"], 1)
        self.assertEqual(row["pdf_size_bytes"], 123)


if __name__ == "__main__":
    unittest.main()
