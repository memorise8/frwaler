# -*- coding: utf-8 -*-
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")


@unittest.skipUnless(TEST_PG_DSN, "set TEST_PG_DSN to run")
class WorkerJobsTest(unittest.TestCase):
    def setUp(self):
        from crawler import db_pg
        from delivery.db import schema
        self.conn = db_pg.open_db(TEST_PG_DSN)
        self.conn.execute("DROP TABLE IF EXISTS crawl_jobs CASCADE")
        self.conn.commit()
        schema.init_delivery_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_enqueue_and_claim(self):
        from delivery.worker import jobs
        jid = jobs.enqueue_job(self.conn, "s1", mode="full", limit_n=5, requested_by="op")
        self.assertIsInstance(jid, int)
        claimed = jobs.claim_next_job(self.conn)
        self.assertEqual(claimed["id"], jid)
        self.assertEqual(claimed["site_id"], "s1")
        self.assertEqual(claimed["mode"], "full")
        self.assertEqual(claimed["limit_n"], 5)

    def test_claim_transitions_to_running(self):
        from delivery.worker import jobs
        jid = jobs.enqueue_job(self.conn, "s1")
        jobs.claim_next_job(self.conn)
        row = self.conn.execute("SELECT status FROM crawl_jobs WHERE id=%s", (jid,)).fetchone()
        self.assertEqual(row["status"], "running")

    def test_claim_empty_returns_none(self):
        from delivery.worker import jobs
        self.assertIsNone(jobs.claim_next_job(self.conn))

    def test_skip_locked_no_double_claim(self):
        from crawler import db_pg
        from delivery.worker import jobs
        jobs.enqueue_job(self.conn, "s1")
        # 두 번째 커넥션이 동시에 잡으려 해도 같은 행을 두 번 잡지 않음
        conn2 = db_pg.open_db(TEST_PG_DSN)
        try:
            a = jobs.claim_next_job(self.conn)
            b = jobs.claim_next_job(conn2)
            self.assertIsNotNone(a)
            self.assertIsNone(b)  # 유일한 queued 작업은 이미 running
        finally:
            conn2.close()

    def test_finish_and_fail(self):
        from delivery.worker import jobs
        jid = jobs.enqueue_job(self.conn, "s1")
        jobs.claim_next_job(self.conn)
        jobs.finish_job(self.conn, jid, saved_count=7)
        row = self.conn.execute("SELECT status, saved_count FROM crawl_jobs WHERE id=%s", (jid,)).fetchone()
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["saved_count"], 7)

        jid2 = jobs.enqueue_job(self.conn, "s2")
        jobs.claim_next_job(self.conn)
        jobs.fail_job(self.conn, jid2, error="boom")
        row2 = self.conn.execute("SELECT status, error FROM crawl_jobs WHERE id=%s", (jid2,)).fetchone()
        self.assertEqual(row2["status"], "failed")
        self.assertEqual(row2["error"], "boom")


if __name__ == "__main__":
    unittest.main()
