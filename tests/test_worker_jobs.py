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
        self.conn.execute("DROP TABLE IF EXISTS crawl_job_logs CASCADE")
        self.conn.execute("DROP TABLE IF EXISTS crawl_schedules CASCADE")
        self.conn.execute("DROP TABLE IF EXISTS crawl_jobs CASCADE")
        self.conn.commit()
        db_pg.init_db(self.conn)
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
        logs=self.conn.execute("SELECT event FROM crawl_job_logs WHERE job_id=%s ORDER BY id",(jid,)).fetchall()
        self.assertEqual([row["event"] for row in logs],["queued","started"])

    def test_summarize_queue_counts_every_status_including_zero(self):
        from delivery.worker import jobs
        jobs.enqueue_job(self.conn, "s1", requested_by="op")
        jobs.enqueue_job(self.conn, "s2", requested_by="op")
        summary = jobs.summarize_queue(self.conn)
        self.assertEqual(summary["counts"]["queued"], 2)
        # 0인 상태도 키가 있어야 화면이 "실행 중 0건"을 말할 수 있다.
        for status in ("queued", "running", "cancelling", "done", "failed", "cancelled"):
            self.assertIn(status, summary["counts"])
        self.assertEqual(summary["counts"]["done"], 0)
        self.assertEqual(summary["active"], 2)

    def test_summarize_queue_needs_three_samples_before_estimating(self):
        from delivery.worker import jobs
        for index in range(2):
            job_id = jobs.enqueue_job(self.conn, f"done{index}", requested_by="op")
            self.conn.execute(
                """UPDATE crawl_jobs SET status='done',
                     started_at=now()-interval '30 seconds', finished_at=now() WHERE id=%s""",
                (job_id,))
        self.conn.commit()
        summary = jobs.summarize_queue(self.conn)
        self.assertEqual(summary["samples"], 2)
        # 표본 2건으로 "약 5시간"을 말하면 그건 추정이 아니라 추측이다.
        self.assertIsNone(summary["avg_seconds"])

    def test_summarize_queue_estimates_from_completed_runs_only(self):
        from delivery.worker import jobs
        for index in range(3):
            job_id = jobs.enqueue_job(self.conn, f"done{index}", requested_by="op")
            self.conn.execute(
                """UPDATE crawl_jobs SET status='done',
                     started_at=now()-interval '20 seconds', finished_at=now() WHERE id=%s""",
                (job_id,))
        # 대기 중 취소된 작업: started_at 이 없다. started_at IS NOT NULL 필터만으로도
        # 걸러지므로, 이 행 하나만으로는 status='done' 조건이 실제로 pin되는지
        # 검증하지 못한다.
        cancelled = jobs.enqueue_job(self.conn, "cancelled-in-queue", requested_by="op")
        self.conn.execute(
            "UPDATE crawl_jobs SET status='cancelled', finished_at=now() WHERE id=%s", (cancelled,))
        # 실행 중 취소된 작업: started_at 이 있고 소요 시간도 1시간으로 크다.
        # status='done' 필터가 빠지면 이 행이 표본에 들어와 평균이 20초에서 크게
        # 벗어나므로, status 필터가 실제로 적용되는지를 이 행이 증명한다.
        cancelled_after_start = jobs.enqueue_job(self.conn, "cancelled-after-start", requested_by="op")
        self.conn.execute(
            """UPDATE crawl_jobs SET status='cancelled',
                 started_at=now()-interval '1 hour', finished_at=now() WHERE id=%s""",
            (cancelled_after_start,))
        self.conn.commit()
        summary = jobs.summarize_queue(self.conn)
        self.assertEqual(summary["samples"], 3)
        self.assertAlmostEqual(summary["avg_seconds"], 20.0, delta=2.0)
        self.assertEqual(summary["finished_24h"], 5)
        self.assertEqual(summary["active"], 0)

    def test_schedule_enqueues_due_once(self):
        from delivery.worker import schedules
        from crawler import db_pg
        db_pg.upsert_site(self.conn,"scheduled","Scheduled","https://scheduled.invalid")
        schedules.upsert(self.conn,site_id="scheduled",interval_hours=24,limit_n=3,created_by="test")
        self.conn.execute("UPDATE crawl_schedules SET next_run_at=now()-interval '1 minute'");self.conn.commit()
        self.assertEqual(schedules.enqueue_due(self.conn),1)
        self.conn.execute("UPDATE crawl_schedules SET next_run_at=now()-interval '1 minute'");self.conn.commit()
        self.assertEqual(schedules.enqueue_due(self.conn),0)
        self.assertEqual(self.conn.execute("SELECT count(*) AS n FROM crawl_jobs WHERE site_id='scheduled'").fetchone()["n"],1)

    def test_schedule_does_not_overlap_cancelling_job(self):
        from delivery.worker import jobs, schedules
        from crawler import db_pg
        db_pg.upsert_site(self.conn,"scheduled","Scheduled","https://scheduled.invalid")
        schedules.upsert(self.conn,site_id="scheduled",interval_hours=24,created_by="test")
        jid=jobs.enqueue_job(self.conn,"scheduled")
        jobs.claim_next_job(self.conn)
        self.assertEqual(jobs.cancel_job(self.conn,jid)["status"],"cancelling")
        self.conn.execute("UPDATE crawl_schedules SET next_run_at=now()-interval '1 minute'")
        self.conn.commit()
        self.assertEqual(schedules.enqueue_due(self.conn),0)
        self.assertEqual(self.conn.execute("SELECT count(*) AS n FROM crawl_jobs WHERE site_id='scheduled'").fetchone()["n"],1)

    def test_delete_schedule_removes_it_and_reports_whether_it_existed(self):
        from delivery.worker import schedules
        from crawler import db_pg
        db_pg.upsert_site(self.conn, "doomed", "Doomed", "https://doomed.invalid")
        schedules.upsert(self.conn, site_id="doomed", interval_hours=24, created_by="test")
        self.assertTrue(schedules.delete(self.conn, "doomed"))
        self.assertEqual(schedules.list_schedules(self.conn), [])
        # 두 번째 호출은 오류가 아니라 "없었다"이다.
        self.assertFalse(schedules.delete(self.conn, "doomed"))

    # 예약을 지우는 것과 이미 등록된 작업을 취소하는 것은 다른 행동이다.
    # 삭제가 조용히 진행 중인 수집을 죽이면 안 된다.
    def test_delete_schedule_leaves_the_jobs_it_already_created(self):
        from delivery.worker import schedules
        from crawler import db_pg
        db_pg.upsert_site(self.conn, "doomed", "Doomed", "https://doomed.invalid")
        schedules.upsert(self.conn, site_id="doomed", interval_hours=24, created_by="test")
        self.conn.execute("UPDATE crawl_schedules SET next_run_at=now() WHERE site_id='doomed'")
        self.conn.commit()
        self.assertEqual(schedules.enqueue_due(self.conn), 1)
        schedules.delete(self.conn, "doomed")
        # 행이 남아 있는 것만으로는 부족하다. 예약 삭제가 진행 중인 수집을 조용히
        # 취소해 버려도 행 수는 그대로 1이므로, 상태와 취소 요청 시각까지 봐야
        # "건드리지 않았다"가 증명된다.
        rows = self.conn.execute(
            "SELECT status, cancel_requested_at FROM crawl_jobs WHERE site_id='doomed'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "queued")
        self.assertIsNone(rows[0]["cancel_requested_at"])

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

    def test_sql_failure_rolls_back_and_records_failed_state(self):
        from delivery.worker import jobs, worker
        class SqlFailureCrawler:
            def __init__(self, db_conn, delay): self.conn = db_conn
            def crawl(self, limit=None): self.conn.execute("SELECT * FROM deliberately_missing_table")
        jid = jobs.enqueue_job(self.conn, "sql-failure")
        self.assertTrue(worker.run_once(self.conn, {"sql-failure": SqlFailureCrawler}, delay=0))
        row = self.conn.execute("SELECT status,error,finished_at FROM crawl_jobs WHERE id=%s",(jid,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("UndefinedTable", row["error"])
        self.assertIsNotNone(row["finished_at"])

    def test_expired_lease_is_requeued_then_failed_at_attempt_limit(self):
        from delivery.worker import jobs
        jid = jobs.enqueue_job(self.conn, "s1")
        jobs.claim_next_job(self.conn, worker_id="dead-worker", lease_seconds=1)
        self.conn.execute("UPDATE crawl_jobs SET lease_expires_at=now()-interval '1 second'")
        self.conn.commit()
        self.assertEqual(jobs.recover_stale(self.conn), 1)
        row = self.conn.execute("SELECT status,worker_id FROM crawl_jobs WHERE id=%s",(jid,)).fetchone()
        self.assertEqual(row["status"], "queued")
        self.assertIsNone(row["worker_id"])
        self.conn.execute("UPDATE crawl_jobs SET status='running',attempts=max_attempts,lease_expires_at=NULL WHERE id=%s",(jid,))
        self.conn.commit()
        self.assertEqual(jobs.recover_stale(self.conn), 1)
        row = self.conn.execute("SELECT status,error FROM crawl_jobs WHERE id=%s",(jid,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], "worker lease expired")

    def test_list_jobs_newest_first_and_filter(self):
        from delivery.worker import jobs
        first = jobs.enqueue_job(self.conn, "s1")
        second = jobs.enqueue_job(self.conn, "s2")
        jobs.claim_next_job(self.conn)
        rows = jobs.list_jobs(self.conn)
        self.assertEqual([row["id"] for row in rows], [second, first])
        running = jobs.list_jobs(self.conn, status="running")
        self.assertEqual([row["id"] for row in running], [first])

    def test_cancel_queued_and_running_job(self):
        from delivery.worker import jobs
        queued = jobs.enqueue_job(self.conn, "s1")
        cancelled = jobs.cancel_job(self.conn, queued)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNotNone(cancelled["finished_at"])
        self.assertIsNone(jobs.cancel_job(self.conn, queued))

        running = jobs.enqueue_job(self.conn, "s2")
        jobs.claim_next_job(self.conn)
        requested = jobs.cancel_job(self.conn, running)
        self.assertEqual(requested["status"], "cancelling")
        self.assertIsNotNone(requested["cancel_requested_at"])
        jobs.finish_job(self.conn, running, 2)
        stopped = self.conn.execute("SELECT status,saved_count FROM crawl_jobs WHERE id=%s",(running,)).fetchone()
        self.assertEqual(stopped["status"], "cancelled")
        self.assertEqual(stopped["saved_count"], 2)

    def test_retry_preserves_source_and_can_only_happen_once(self):
        from delivery.worker import jobs
        source = jobs.enqueue_job(self.conn, "s1", mode="full", limit_n=3,
                                  requested_by="operator")
        jobs.claim_next_job(self.conn)
        jobs.fail_job(self.conn, source, "boom")
        retried = jobs.retry_job(self.conn, source, requested_by="retry-button")
        self.assertEqual(retried["status"], "queued")
        self.assertEqual(retried["retry_of"], source)
        self.assertEqual(retried["site_id"], "s1")
        self.assertEqual(retried["mode"], "full")
        self.assertEqual(retried["limit_n"], 3)
        self.assertEqual(retried["requested_by"], "retry-button")
        old = self.conn.execute(
            "SELECT status, error, retried_by FROM crawl_jobs WHERE id=%s", (source,)
        ).fetchone()
        self.assertEqual(old["status"], "failed")
        self.assertEqual(old["error"], "boom")
        self.assertEqual(old["retried_by"], retried["id"])
        self.assertIsNone(jobs.retry_job(self.conn, source))

    def test_retry_rejects_non_terminal_job(self):
        from delivery.worker import jobs
        queued = jobs.enqueue_job(self.conn, "s1")
        self.assertIsNone(jobs.retry_job(self.conn, queued))

    def test_finish_job_records_truncation(self):
        from delivery.worker import jobs
        cut = jobs.enqueue_job(self.conn, "cut", requested_by="op")
        whole = jobs.enqueue_job(self.conn, "whole", requested_by="op")
        for jid in (cut, whole):
            self.conn.execute("UPDATE crawl_jobs SET status='running' WHERE id=%s", (jid,))
        self.conn.commit()
        jobs.finish_job(self.conn, cut, saved_count=5, truncated=True)
        jobs.finish_job(self.conn, whole, saved_count=5)
        rows = {r["site_id"]: r for r in self.conn.execute(
            "SELECT site_id, status, truncated FROM crawl_jobs").fetchall()}
        self.assertTrue(rows["cut"]["truncated"])
        self.assertFalse(rows["whole"]["truncated"])
        # 잘렸어도 저장한 문서는 유효하다. status 는 여전히 done 이어야 한다.
        self.assertEqual(rows["cut"]["status"], "done")

    def test_progress_roundtrip_accumulates_items(self):
        from delivery.worker import jobs
        jobs.save_progress(self.conn, "rc-fake-site", {"page": 5}, items_delta=100)
        jobs.save_progress(self.conn, "rc-fake-site", {"page": 9}, items_delta=50)
        cur = jobs.load_cursor(self.conn, "rc-fake-site")
        self.assertEqual(cur, {"page": 9})
        row = self.conn.execute(
            "SELECT items_done, completed_at FROM crawl_site_progress WHERE site_id=%s",
            ("rc-fake-site",)).fetchone()
        self.assertEqual(row["items_done"], 150)
        self.assertIsNone(row["completed_at"])

    def test_load_cursor_returns_none_for_unknown_site(self):
        from delivery.worker import jobs
        self.assertIsNone(jobs.load_cursor(self.conn, "rc-never-seen"))

    def test_mark_complete_keeps_cursor(self):
        from delivery.worker import jobs
        # oldest_first 증분이 커서를 이어 쓰므로, 완주가 커서를 지우면 안 된다.
        jobs.save_progress(self.conn, "rc-done-site", {"offset": 900}, items_delta=900)
        jobs.mark_backfill_complete(self.conn, "rc-done-site")
        self.assertEqual(jobs.load_cursor(self.conn, "rc-done-site"), {"offset": 900})
        row = self.conn.execute(
            "SELECT completed_at FROM crawl_site_progress WHERE site_id=%s",
            ("rc-done-site",)).fetchone()
        self.assertIsNotNone(row["completed_at"])


if __name__ == "__main__":
    unittest.main()
