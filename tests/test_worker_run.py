# -*- coding: utf-8 -*-
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")


class _FakeCrawler:
    """crawl() 시 PG 에 문서 2건 저장하는 가짜 크롤러."""
    site_id = "fake"
    site_name = "Fake"
    base_url = "https://fake.example"

    def __init__(self, db_conn, delay=1.0):
        self._conn = db_conn
        self._delay = delay

    def crawl(self, limit=None):
        from crawler import db_pg
        db_pg.insert_document(self._conn, {"site_id": "fake", "post_number": "1",
                                           "meta_url": "https://fake/1", "title": "A"})
        db_pg.insert_document(self._conn, {"site_id": "fake", "post_number": "2",
                                           "meta_url": "https://fake/2", "title": "B"})


@unittest.skipUnless(TEST_PG_DSN, "set TEST_PG_DSN to run")
class WorkerRunTest(unittest.TestCase):
    def setUp(self):
        from crawler import db_pg
        from delivery.db import schema
        os.environ["LIBERTREE_DB_BACKEND"] = "postgres"
        self.db_pg = db_pg
        self.conn = db_pg.open_db(TEST_PG_DSN)
        for t in ("crawl_jobs", "documents", "sites"):
            self.conn.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
        self.conn.commit()
        db_pg.init_db(self.conn)
        schema.init_delivery_schema(self.conn)
        db_pg.upsert_site(self.conn, "fake", "Fake", "https://fake.example")

    def tearDown(self):
        os.environ.pop("LIBERTREE_DB_BACKEND", None)
        self.conn.close()

    def test_run_job_saves_and_finishes(self):
        from delivery.worker import jobs, worker
        jid = jobs.enqueue_job(self.conn, "fake", mode="full")
        job = jobs.claim_next_job(self.conn)
        saved = worker.run_job(self.conn, job, crawler_registry={"fake": _FakeCrawler}, delay=0)
        self.assertEqual(saved, 2)
        row = self.conn.execute("SELECT status, saved_count FROM crawl_jobs WHERE id=%s", (jid,)).fetchone()
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["saved_count"], 2)

    def test_worker_propagates_requested_mode_to_crawler(self):
        from delivery.worker import jobs, worker
        seen=[]
        class ModeCrawler(_FakeCrawler):
            def crawl(self,limit=None): seen.append(self.delivery_mode)
        jobs.enqueue_job(self.conn,"fake",mode="incremental")
        worker.run_job(self.conn,jobs.claim_next_job(self.conn),{"fake":ModeCrawler},delay=0)
        self.assertEqual(seen,["incremental"])

    def test_base_crawler_stops_at_safe_cancellation_boundary(self):
        from crawler.base_crawler import BaseCrawler
        from delivery.worker import jobs, worker
        class CancellableCrawler(BaseCrawler):
            site_id="fake";site_name="Fake";base_url="https://fake.example"
            def crawl(self,limit=None): self._check_cancelled()
        jid=jobs.enqueue_job(self.conn,"fake",mode="incremental")
        job=jobs.claim_next_job(self.conn)
        requested=jobs.cancel_job(self.conn,jid)
        self.assertEqual(requested["status"],"cancelling")
        worker.run_job(self.conn,job,{"fake":CancellableCrawler},delay=0,should_cancel=lambda:True)
        row=self.conn.execute("SELECT status,finished_at FROM crawl_jobs WHERE id=%s",(jid,)).fetchone()
        self.assertEqual(row["status"],"cancelled")
        self.assertIsNotNone(row["finished_at"])

    def test_incremental_rerun_does_not_duplicate_documents(self):
        from delivery.worker import jobs, worker

        first = jobs.enqueue_job(self.conn, "fake", mode="full")
        worker.run_job(
            self.conn,
            jobs.claim_next_job(self.conn),
            crawler_registry={"fake": _FakeCrawler},
            delay=0,
        )
        incremental = jobs.enqueue_job(self.conn, "fake", mode="incremental")
        saved = worker.run_job(
            self.conn,
            jobs.claim_next_job(self.conn),
            crawler_registry={"fake": _FakeCrawler},
            delay=0,
        )

        rows = self.conn.execute(
            "SELECT id, status, saved_count FROM crawl_jobs WHERE id IN (%s, %s) ORDER BY id",
            (first, incremental),
        ).fetchall()
        documents = self.conn.execute(
            "SELECT count(*) AS n FROM documents WHERE site_id=%s", ("fake",)
        ).fetchone()
        self.assertEqual(saved, 0)
        self.assertEqual(documents["n"], 2)
        self.assertEqual(
            [(row["status"], row["saved_count"]) for row in rows],
            [("done", 2), ("done", 0)],
        )

    def test_run_job_unknown_site_fails(self):
        from delivery.worker import jobs, worker
        jid = jobs.enqueue_job(self.conn, "nope")
        job = jobs.claim_next_job(self.conn)
        worker.run_job(self.conn, job, crawler_registry={}, delay=0)
        row = self.conn.execute("SELECT status, error FROM crawl_jobs WHERE id=%s", (jid,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("nope", row["error"])

    def test_run_once_processes_one(self):
        from delivery.worker import jobs, worker
        jobs.enqueue_job(self.conn, "fake", mode="full")
        did = worker.run_once(self.conn, crawler_registry={"fake": _FakeCrawler}, delay=0)
        self.assertTrue(did)
        self.assertFalse(worker.run_once(self.conn, crawler_registry={"fake": _FakeCrawler}, delay=0))


if __name__ == "__main__":
    unittest.main()
