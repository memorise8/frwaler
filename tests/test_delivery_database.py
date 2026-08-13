import os
import unittest

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")


@unittest.skipUnless(TEST_PG_DSN, "set TEST_PG_DSN to run")
class DeliveryDatabaseTest(unittest.TestCase):
    def test_pool_applies_query_and_idle_transaction_timeouts(self):
        from delivery.be.database import get_database

        conn = get_database(TEST_PG_DSN).connection()
        try:
            statement_timeout = conn.execute("SHOW statement_timeout").fetchone()[
                "statement_timeout"
            ]
            idle_timeout = conn.execute(
                "SHOW idle_in_transaction_session_timeout"
            ).fetchone()["idle_in_transaction_session_timeout"]
        finally:
            conn.close()

        self.assertEqual(statement_timeout, "30s")
        self.assertEqual(idle_timeout, "30s")

    def test_returned_connection_is_clean_for_next_borrower(self):
        from psycopg.pq import TransactionStatus

        from delivery.be.database import get_database

        database = get_database(TEST_PG_DSN)
        first = database.connection()
        first.execute("SELECT 1").fetchone()
        first.close()

        second = database.connection()
        try:
            self.assertEqual(second.info.transaction_status, TransactionStatus.IDLE)
        finally:
            second.close()


if __name__ == "__main__":
    unittest.main()
