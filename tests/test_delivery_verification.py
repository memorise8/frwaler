# -*- coding: utf-8 -*-
import os
import unittest

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")


@unittest.skipUnless(TEST_PG_DSN, "set TEST_PG_DSN to run")
class CollectVerificationTest(unittest.TestCase):
    def setUp(self):
        from crawler import db_pg
        from delivery.db import schema
        self.conn = db_pg.open_db(TEST_PG_DSN)
        self.conn.execute("DROP SCHEMA public CASCADE")
        self.conn.execute("CREATE SCHEMA public")
        self.conn.commit()
        db_pg.init_db(self.conn)
        schema.init_delivery_schema(self.conn)
        for site in ("works", "blocked", "quiet"):
            db_pg.upsert_site(self.conn, site, site, f"https://{site}.example")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _job(self, site_id, status, saved_count=0, error=None):
        from delivery.worker import jobs
        job_id = jobs.enqueue_job(self.conn, site_id)
        jobs.claim_next_job(self.conn)
        if status == "failed":
            jobs.fail_job(self.conn, job_id, error or "boom")
        else:
            jobs.finish_job(self.conn, job_id, saved_count=saved_count)
        return job_id

    def _by_site(self):
        from delivery.be.verification import collect_verification
        return {row["site_id"]: row for row in collect_verification(self.conn)["sites"]}

    def test_reports_the_latest_job_for_each_site(self):
        self._job("works", "done", saved_count=5)
        self._job("works", "done", saved_count=0)
        row = self._by_site()["works"]
        self.assertEqual(row["last_status"], "done")
        self.assertEqual(row["last_saved_count"], 0)
        self.assertEqual(row["jobs"], 2)

    # The point of the whole aggregate: one successful run anywhere in the
    # history proves the crawler reaches its source from this network, even if
    # the newest run added nothing and even if the audit calls it broken.
    def test_remembers_that_a_site_once_saved_documents(self):
        self._job("works", "done", saved_count=5)
        self._job("works", "done", saved_count=0)
        self.assertEqual(self._by_site()["works"]["best_saved_count"], 5)

    def test_carries_the_failure_reason_of_the_latest_job(self):
        self._job("blocked", "failed", error="HTTPError: 403")
        row = self._by_site()["blocked"]
        self.assertEqual(row["last_status"], "failed")
        self.assertIn("403", row["last_error"])
        self.assertEqual(row["best_saved_count"], 0)

    def test_omits_sites_that_have_never_run_here(self):
        self._job("works", "done", saved_count=1)
        self.assertNotIn("quiet", self._by_site())

    def test_summarises_how_much_this_deployment_has_proven(self):
        from delivery.be.verification import collect_verification
        self._job("works", "done", saved_count=3)
        self._job("blocked", "failed")
        summary = collect_verification(self.conn)["summary"]
        self.assertEqual(summary["sites_with_jobs"], 2)
        self.assertEqual(summary["sites_with_saved_documents"], 1)

    def test_empty_database_reports_nothing_rather_than_failing(self):
        from delivery.be.verification import collect_verification
        result = collect_verification(self.conn)
        self.assertEqual(result["sites"], [])
        self.assertEqual(result["summary"]["sites_with_jobs"], 0)

    def test_timestamps_come_back_timezone_aware(self):
        self._job("works", "done", saved_count=1)
        finished = self._by_site()["works"]["last_finished_at"]
        self.assertIsNotNone(finished)
        self.assertIsNotNone(finished.tzinfo)


if __name__ == "__main__":
    unittest.main()
