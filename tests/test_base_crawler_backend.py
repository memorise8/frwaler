# -*- coding: utf-8 -*-
"""base_crawler 가 backend=postgres 일 때 PG 로 저장하는지 + sqlite 기본 회귀 확인."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")


class _FakeCrawler:
    """base_crawler 의 저장 경로만 재사용하기 위한 최소 크롤러."""
    site_id = "fake"
    site_name = "Fake Site"
    base_url = "https://fake.example"

    def crawl(self, limit=None):  # not used here
        pass


def _make_instance(conn):
    from crawler.base_crawler import BaseCrawler

    # BaseCrawler 는 ABC 이므로 추상 메서드를 채운 서브클래스를 즉석 생성
    Concrete = type("Concrete", (BaseCrawler,), {
        "site_id": property(lambda self: "fake"),
        "site_name": property(lambda self: "Fake Site"),
        "base_url": property(lambda self: "https://fake.example"),
        "crawl": lambda self, limit=None: None,
    })
    return Concrete(db_conn=conn, delay=0)


@unittest.skipUnless(TEST_PG_DSN, "set TEST_PG_DSN to run")
class BaseCrawlerPostgresTest(unittest.TestCase):
    def setUp(self):
        from crawler import db_pg
        os.environ["LIBERTREE_DB_BACKEND"] = "postgres"
        self.db_pg = db_pg
        self.conn = db_pg.open_db(TEST_PG_DSN)
        self.conn.execute("DROP TABLE IF EXISTS documents CASCADE")
        self.conn.execute("DROP TABLE IF EXISTS sites CASCADE")
        self.conn.commit()
        db_pg.init_db(self.conn)
        db_pg.upsert_site(self.conn, "fake", "Fake Site", "https://fake.example")

    def tearDown(self):
        os.environ.pop("LIBERTREE_DB_BACKEND", None)
        self.conn.close()

    def test_save_paper_v2_writes_to_postgres(self):
        inst = _make_instance(self.conn)
        seq = inst._save_paper_v2({
            "site_id": "fake", "post_number": "1",
            "meta_url": "https://fake/a", "title": "T",
        })
        self.assertIsInstance(seq, int)
        row = self.db_pg.get_document(self.conn, seq)
        self.assertEqual(row["title"], "T")


class BaseCrawlerSqliteRegressionTest(unittest.TestCase):
    def test_default_backend_is_sqlite(self):
        # 회귀 가드: 환경변수 없으면 백엔드 셀렉터가 SQLite 를 돌려줘야 함
        os.environ.pop("LIBERTREE_DB_BACKEND", None)
        from crawler import db_backend, db_libertree
        self.assertIs(db_backend.get_backend(), db_libertree)


if __name__ == "__main__":
    unittest.main()
