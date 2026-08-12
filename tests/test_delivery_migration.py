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


if __name__ == "__main__":
    unittest.main()
