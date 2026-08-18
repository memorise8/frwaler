import os
import unittest

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")


@unittest.skipUnless(TEST_PG_DSN, "set TEST_PG_DSN to run")
class DeliveryMigrationTest(unittest.TestCase):
    def setUp(self):
        from crawler import db_pg
        self.conn = db_pg.open_db(TEST_PG_DSN)
        self.conn.execute("DROP SCHEMA public CASCADE")
        self.conn.execute("CREATE SCHEMA public")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_explicit_migration_builds_complete_serving_schema(self):
        from delivery.be.migrate import migrate
        from delivery.db.schema import verify_required_schema
        migrate(TEST_PG_DSN)
        verify_required_schema(self.conn)
        self.assertIsNotNone(self.conn.execute(
            "SELECT fts FROM documents LIMIT 1"
        ).description)

    def test_verifier_rejects_unmigrated_database(self):
        from crawler import db_pg
        from delivery.db.schema import verify_required_schema
        db_pg.init_db(self.conn)
        with self.assertRaisesRegex(RuntimeError, "document_lang"):
            verify_required_schema(self.conn)

    def test_truncated_column_exists_and_defaults_false(self):
        from delivery.be.migrate import migrate
        migrate(TEST_PG_DSN)
        row = self.conn.execute("""SELECT data_type, column_default, is_nullable
              FROM information_schema.columns
             WHERE table_schema='public' AND table_name='crawl_jobs' AND column_name='truncated'""").fetchone()
        self.assertIsNotNone(row, "crawl_jobs.truncated 이 없다")
        self.assertEqual(row["data_type"], "boolean")
        self.assertEqual(row["is_nullable"], "NO")
        self.assertIn("false", (row["column_default"] or "").lower())

    # 컬럼이 빠진 데이터베이스로 BE 가 조용히 뜨면 안 된다 — 다른 필수 컬럼과 같은 취급.
    def test_missing_truncated_column_fails_schema_verification(self):
        from delivery.be.migrate import migrate
        from delivery.db.schema import verify_required_schema
        migrate(TEST_PG_DSN)
        verify_required_schema(self.conn)          # 먼저 통과하는지 확인
        self.conn.execute("ALTER TABLE crawl_jobs DROP COLUMN truncated")
        self.conn.commit()
        with self.assertRaisesRegex(RuntimeError, r"crawl_jobs\.truncated"):
            verify_required_schema(self.conn)

    # 이미 행이 있는 기존 설치에서도 추가가 안전해야 한다.
    def test_truncated_column_backfills_existing_rows_as_false(self):
        from delivery.be.migrate import migrate
        from delivery.db import schema
        migrate(TEST_PG_DSN)
        self.conn.execute("ALTER TABLE crawl_jobs DROP COLUMN truncated")
        self.conn.execute("INSERT INTO crawl_jobs(site_id, status) VALUES ('legacy','done')")
        self.conn.commit()
        schema.init_delivery_schema(self.conn)     # 재실행 = 마이그레이션
        row = self.conn.execute("SELECT truncated FROM crawl_jobs WHERE site_id='legacy'").fetchone()
        self.assertFalse(row["truncated"])

    def test_crawl_site_progress_table_exists_with_required_columns(self):
        from delivery.be.migrate import migrate
        migrate(TEST_PG_DSN)
        cols = {r["column_name"] for r in self.conn.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name='crawl_site_progress'").fetchall()}
        self.assertLessEqual(
            {"site_id", "cursor", "items_done", "total_estimate",
             "updated_at", "completed_at"}, cols)

    def test_migration_is_idempotent_for_progress_table(self):
        from delivery.be.migrate import migrate
        from delivery.db import schema
        migrate(TEST_PG_DSN)
        # 두 번 돌려도 무해해야 기존 설치에 안전하다.
        schema.init_delivery_schema(self.conn)
        schema.init_delivery_schema(self.conn)


if __name__ == "__main__":
    unittest.main()
