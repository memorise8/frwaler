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
        # crawl_job_logs must be dropped too: DROP TABLE crawl_jobs CASCADE
        # removes the foreign key but leaves the log rows behind, and job ids
        # restart at 1 in every test, so leftovers would attach themselves to
        # the next test's job.
        for t in ("crawl_job_logs", "crawl_jobs", "documents", "sites"):
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

    # --- job duration and timeline -------------------------------------
    # PostgreSQL now() is transaction_timestamp(), frozen at BEGIN. run_job
    # holds a single transaction for the whole crawl whenever nothing is
    # saved, so finished_at used to be stamped with the moment the
    # transaction opened: a 3m54s blocked crawl was recorded as 0s.

    def test_duration_is_measured_even_when_the_crawl_saves_nothing(self):
        import time as _time
        from delivery.worker import jobs, worker

        class SlowEmptyCrawler(_FakeCrawler):
            def crawl(self, limit=None):
                _time.sleep(1.2)

        jid = jobs.enqueue_job(self.conn, "fake")
        worker.run_job(self.conn, jobs.claim_next_job(self.conn),
                       {"fake": SlowEmptyCrawler}, delay=0)
        row = self.conn.execute(
            "SELECT saved_count, EXTRACT(EPOCH FROM (finished_at-started_at)) AS secs"
            " FROM crawl_jobs WHERE id=%s", (jid,)).fetchone()
        self.assertEqual(row["saved_count"], 0)
        self.assertGreaterEqual(float(row["secs"]), 1.0)

    def test_job_log_timeline_does_not_collapse_to_one_instant(self):
        import time as _time
        from delivery.worker import jobs, worker

        class SlowEmptyCrawler(_FakeCrawler):
            def crawl(self, limit=None):
                _time.sleep(1.2)

        jid = jobs.enqueue_job(self.conn, "fake")
        worker.run_job(self.conn, jobs.claim_next_job(self.conn),
                       {"fake": SlowEmptyCrawler}, delay=0)
        rows = self.conn.execute(
            "SELECT event, created_at FROM crawl_job_logs WHERE job_id=%s ORDER BY id",
            (jid,)).fetchall()
        stamps = {r["event"]: r["created_at"] for r in rows}
        self.assertIn("started", stamps)
        self.assertIn("completed", stamps)
        self.assertGreaterEqual(
            (stamps["completed"] - stamps["started"]).total_seconds(), 1.0)

    # --- crawler output ------------------------------------------------
    # 703 of the 804 crawlers shell out to curl through their own private
    # helper and only 14 use BaseCrawler._request, so stdout is the single
    # thing they all share. Without it the console cannot tell a blocked
    # crawl from a successful one: both report 신규 0건.

    def test_crawler_stdout_is_recorded_against_the_job(self):
        from delivery.worker import jobs, worker

        class TalkingCrawler(_FakeCrawler):
            def crawl(self, limit=None):
                print("[fake] fetch attempt 1/3 failed: all_layers_failed")
                print("[fake] done. Total saved: 0")

        jid = jobs.enqueue_job(self.conn, "fake")
        worker.run_job(self.conn, jobs.claim_next_job(self.conn),
                       {"fake": TalkingCrawler}, delay=0)
        messages = [r["message"] for r in self.conn.execute(
            "SELECT message FROM crawl_job_logs WHERE job_id=%s AND event='crawler_output'"
            " ORDER BY id", (jid,)).fetchall()]
        self.assertEqual(messages, ["[fake] fetch attempt 1/3 failed: all_layers_failed",
                                    "[fake] done. Total saved: 0"])

    def test_crawler_stdout_survives_a_crash_and_keeps_the_failure(self):
        from delivery.worker import jobs, worker

        class ExplodingCrawler(_FakeCrawler):
            def crawl(self, limit=None):
                print("[fake] list page 1/4: discovered 6 records")
                raise ValueError("boom")

        jid = jobs.enqueue_job(self.conn, "fake")
        worker.run_job(self.conn, jobs.claim_next_job(self.conn),
                       {"fake": ExplodingCrawler}, delay=0)
        row = self.conn.execute("SELECT status, error FROM crawl_jobs WHERE id=%s",
                                (jid,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("boom", row["error"])
        messages = [r["message"] for r in self.conn.execute(
            "SELECT message FROM crawl_job_logs WHERE job_id=%s AND event='crawler_output'",
            (jid,)).fetchall()]
        self.assertEqual(messages, ["[fake] list page 1/4: discovered 6 records"])

    def test_only_the_tail_is_kept_so_a_long_crawl_cannot_flood_the_table(self):
        from delivery.worker import jobs, worker

        class ChattyCrawler(_FakeCrawler):
            def crawl(self, limit=None):
                for index in range(worker.CRAWL_OUTPUT_LINES + 25):
                    print(f"[fake] line {index}")

        jid = jobs.enqueue_job(self.conn, "fake")
        worker.run_job(self.conn, jobs.claim_next_job(self.conn),
                       {"fake": ChattyCrawler}, delay=0)
        messages = [r["message"] for r in self.conn.execute(
            "SELECT message FROM crawl_job_logs WHERE job_id=%s AND event='crawler_output'"
            " ORDER BY id", (jid,)).fetchall()]
        self.assertEqual(len(messages), worker.CRAWL_OUTPUT_LINES)
        # The tail is what diagnoses a run, so the newest lines are the ones kept.
        self.assertEqual(messages[-1],
                         f"[fake] line {worker.CRAWL_OUTPUT_LINES + 24}")

    def test_crawler_output_still_reaches_the_container_log(self):
        import io as _io
        from delivery.worker import jobs, worker

        class TalkingCrawler(_FakeCrawler):
            def crawl(self, limit=None):
                print("[fake] visible in docker logs")

        buffer = _io.StringIO()
        real_stdout, sys.stdout = sys.stdout, buffer
        try:
            jobs.enqueue_job(self.conn, "fake")
            worker.run_job(self.conn, jobs.claim_next_job(self.conn),
                           {"fake": TalkingCrawler}, delay=0)
        finally:
            sys.stdout = real_stdout
        self.assertIn("[fake] visible in docker logs", buffer.getvalue())


    # --- crawler output retention --------------------------------------
    # Capturing stdout took a job from 3 log rows to ~43. A single 804-site
    # bulk sweep therefore writes about 32,000 rows, and nothing pruned this
    # table -- translation_system_observations had a retention pass, this did
    # not. The job row and its lifecycle log stay: /sites/verification
    # aggregates that history, and losing it to retention would erase the
    # evidence that a crawler ever worked on this install.

    def _age_logs(self, job_id, days):
        self.conn.execute(
            "UPDATE crawl_job_logs SET created_at=clock_timestamp()-make_interval(days=>%s)"
            " WHERE job_id=%s", (days, job_id))
        self.conn.commit()

    def _counts(self, job_id):
        row = self.conn.execute(
            "SELECT count(*) FILTER (WHERE event='crawler_output') AS output,"
            "       count(*) FILTER (WHERE event<>'crawler_output') AS lifecycle"
            "  FROM crawl_job_logs WHERE job_id=%s", (job_id,)).fetchone()
        return int(row["output"]), int(row["lifecycle"])

    def _talking_job(self):
        from delivery.worker import jobs, worker

        class TalkingCrawler(_FakeCrawler):
            def crawl(self, limit=None):
                print("[fake] fetch attempt 1/3 failed: all_layers_failed")
                print("[fake] done. Total saved: 0")

        jid = jobs.enqueue_job(self.conn, "fake")
        worker.run_job(self.conn, jobs.claim_next_job(self.conn), {"fake": TalkingCrawler}, delay=0)
        return jid

    def test_prunes_crawler_output_past_the_retention_window(self):
        from delivery.worker import jobs
        jid = self._talking_job()
        self.assertEqual(self._counts(jid), (2, 3))
        self._age_logs(jid, 30)
        self.assertEqual(jobs.prune_crawler_output(self.conn, retention_days=14), 2)
        self.assertEqual(self._counts(jid), (0, 3))

    def test_keeps_output_inside_the_window(self):
        from delivery.worker import jobs
        jid = self._talking_job()
        self._age_logs(jid, 3)
        self.assertEqual(jobs.prune_crawler_output(self.conn, retention_days=14), 0)
        self.assertEqual(self._counts(jid), (2, 3))

    def test_never_removes_the_lifecycle_trail_or_the_job(self):
        from delivery.worker import jobs
        jid = self._talking_job()
        self._age_logs(jid, 3650)
        jobs.prune_crawler_output(self.conn, retention_days=0)
        output, lifecycle = self._counts(jid)
        self.assertEqual(output, 0)
        self.assertEqual(lifecycle, 3)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM crawl_jobs WHERE id=%s", (jid,)).fetchone())

    def test_pruning_an_empty_table_is_a_no_op(self):
        from delivery.worker import jobs
        self.assertEqual(jobs.prune_crawler_output(self.conn, retention_days=14), 0)

    def test_zero_retention_removes_output_written_moments_ago(self):
        # Retention 0 means keep none, including the run that just finished.
        # Stated here so the behaviour is a decision rather than a surprise.
        from delivery.worker import jobs
        jid = self._talking_job()
        self.assertEqual(jobs.prune_crawler_output(self.conn, retention_days=0), 2)
        self.assertEqual(self._counts(jid), (0, 3))

    def test_a_negative_retention_is_clamped_rather_than_inverted(self):
        # Unclamped, -5 would put the cutoff five days in the FUTURE and delete
        # output no window should touch. Clamped to 0 the cutoff is now, so a
        # future-dated row survives.
        from delivery.worker import jobs
        jid = self._talking_job()
        self.conn.execute(
            "UPDATE crawl_job_logs SET created_at=clock_timestamp()+make_interval(days=>2)"
            " WHERE job_id=%s AND event='crawler_output'", (jid,))
        self.conn.commit()
        self.assertEqual(jobs.prune_crawler_output(self.conn, retention_days=-5), 0)
        self.assertEqual(self._counts(jid), (2, 3))

    # --- truncation ------------------------------------------------------

    def test_slow_crawl_is_recorded_as_truncated(self):
        import time
        from unittest import mock
        from delivery.worker import jobs, worker
        class SlowCrawler(_FakeCrawler):
            def crawl(self, limit=None):
                time.sleep(2.5)
        jid = jobs.enqueue_job(self.conn, "fake", mode="incremental")
        job = jobs.claim_next_job(self.conn)
        with mock.patch.dict(os.environ, {"LIBERTREE_MAX_WALL_S": "2"}):
            worker.run_job(self.conn, job, crawler_registry={"fake": SlowCrawler}, delay=0)
        row = self.conn.execute("SELECT status, truncated FROM crawl_jobs WHERE id=%s", (jid,)).fetchone()
        self.assertEqual(row["status"], "done")
        self.assertTrue(row["truncated"])

    def test_quick_crawl_is_not_truncated(self):
        from unittest import mock
        from delivery.worker import jobs, worker
        jid = jobs.enqueue_job(self.conn, "fake", mode="incremental")
        job = jobs.claim_next_job(self.conn)
        with mock.patch.dict(os.environ, {"LIBERTREE_MAX_WALL_S": "2"}):
            worker.run_job(self.conn, job, crawler_registry={"fake": _FakeCrawler}, delay=0)
        row = self.conn.execute("SELECT truncated FROM crawl_jobs WHERE id=%s", (jid,)).fetchone()
        self.assertFalse(row["truncated"])

    # 오래 돌다 취소된 것과 예산에 잘린 것은 다른 사건이다.
    def test_cancelled_slow_crawl_is_not_marked_truncated(self):
        import time
        from unittest import mock
        from crawler.base_crawler import BaseCrawler
        from delivery.worker import jobs, worker
        class SlowCancellable(BaseCrawler):
            site_id="fake";site_name="Fake";base_url="https://fake.example"
            def crawl(self, limit=None):
                time.sleep(2.5)
                self._check_cancelled()
        jid = jobs.enqueue_job(self.conn, "fake", mode="incremental")
        job = jobs.claim_next_job(self.conn)
        jobs.cancel_job(self.conn, jid)
        with mock.patch.dict(os.environ, {"LIBERTREE_MAX_WALL_S": "2"}):
            worker.run_job(self.conn, job, {"fake": SlowCancellable}, delay=0, should_cancel=lambda: True)
        row = self.conn.execute("SELECT truncated FROM crawl_jobs WHERE id=%s", (jid,)).fetchone()
        self.assertFalse(row["truncated"])

    # 실패는 실패다.
    def test_slow_failing_crawl_is_not_marked_truncated(self):
        import time
        from unittest import mock
        from delivery.worker import jobs, worker
        class SlowBroken(_FakeCrawler):
            def crawl(self, limit=None):
                time.sleep(2.5)
                raise RuntimeError("boom")
        jid = jobs.enqueue_job(self.conn, "fake", mode="incremental")
        job = jobs.claim_next_job(self.conn)
        with mock.patch.dict(os.environ, {"LIBERTREE_MAX_WALL_S": "2"}):
            worker.run_job(self.conn, job, crawler_registry={"fake": SlowBroken}, delay=0)
        row = self.conn.execute("SELECT status, truncated FROM crawl_jobs WHERE id=%s", (jid,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertFalse(row["truncated"])

    def test_truncation_threshold_scales_with_the_budget(self):
        from unittest import mock
        from delivery.worker import worker
        with mock.patch.dict(os.environ, {"LIBERTREE_MAX_WALL_S": "1500"}):
            self.assertAlmostEqual(worker.truncation_threshold_seconds(), 1440.0, places=3)
        # 테스트가 예산을 낮게 잡아도 임계가 음수로 무너지지 않아야 한다.
        with mock.patch.dict(os.environ, {"LIBERTREE_MAX_WALL_S": "2"}):
            self.assertAlmostEqual(worker.truncation_threshold_seconds(), 1.8, places=3)

    # --- cursor contract: up-to-date early stop -------------------------

    def test_incremental_newest_first_stops_after_threshold_known_docs(self):
        # 같은 문서를 임계+1 회 저장 시도하는 가짜 크롤러: 첫 회는 신규 저장,
        # 이후는 기보유 → 연속 카운터가 임계에 닿으면 CrawlUpToDate.
        # 워커 처리(정상 done)는 Task 4 테스트가 고정한다 — 여기서는 크롤러를
        # 직접 호출해 예외가 crawl() 밖으로 나오는지만 확인한다.
        from crawler.base_crawler import BaseCrawler, CrawlUpToDate

        class UpToDateCrawler(BaseCrawler):
            site_id = "rc-uptodate"
            site_name = "RC UpToDate"
            base_url = "https://rc-uptodate.example"
            DELIVERY_ORDER = "newest_first"
            UP_TO_DATE_THRESHOLD = 3

            def crawl(self, limit=None):
                for _ in range(10):
                    self._save_paper_v2({
                        "site_id": self.site_id, "post_number": "fixed-1",
                        "meta_url": "http://invalid.invalid/1", "title": "t"})
                raise AssertionError("threshold 에서 CrawlUpToDate 가 났어야 한다")

        self.db_pg.upsert_site(self.conn, "rc-uptodate", "RC UpToDate", "https://rc-uptodate.example")
        inst = UpToDateCrawler(db_conn=self.conn, delay=0)
        inst.delivery_mode = "incremental"
        with self.assertRaises(CrawlUpToDate):
            inst.crawl()

    # --- cursor contract: inject, persist, auto-requeue, no-advance guard -

    def test_truncated_backfill_saves_cursor_and_requeues_at_tail(self):
        # 커서에서 시작해 두 페이지 걷고 각각 _advance_cursor 보고; 테스트가
        # 예산을 0에 가깝게 줄여 잘림 판정을 강제한다.
        import time as _time
        from unittest import mock
        from crawler.base_crawler import BaseCrawler
        from delivery.worker import jobs, worker

        class PagedBackfillCrawler(BaseCrawler):
            site_id = "rc-backfill-a"
            site_name = "RC Backfill A"
            base_url = "https://rc-backfill-a.example"

            def crawl(self, limit=None):
                page = (self.delivery_cursor or {}).get("page", 1)
                for _ in range(2):
                    _time.sleep(0.02)
                    page += 1
                    self._advance_cursor({"page": page})

        jobs.enqueue_job(self.conn, "rc-backfill-a", mode="backfill")
        job = jobs.claim_next_job(self.conn)
        with mock.patch.dict(os.environ, {"LIBERTREE_MAX_WALL_S": "0.01"}):
            worker.run_job(self.conn, job, crawler_registry={"rc-backfill-a": PagedBackfillCrawler}, delay=0)
        cur = jobs.load_cursor(self.conn, "rc-backfill-a")
        self.assertEqual(cur, {"page": 3})
        tail = self.conn.execute(
            "SELECT site_id, mode, status FROM crawl_jobs ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual((tail["site_id"], tail["mode"], tail["status"]),
                         ("rc-backfill-a", "backfill", "queued"))

    def test_no_advance_means_no_requeue(self):
        # _advance_cursor 를 한 번도 부르지 않는 가짜 크롤러 + 잘림 강제.
        import time as _time
        from unittest import mock
        from crawler.base_crawler import BaseCrawler
        from delivery.worker import jobs, worker

        class StuckBackfillCrawler(BaseCrawler):
            site_id = "rc-backfill-stuck"
            site_name = "RC Backfill Stuck"
            base_url = "https://rc-backfill-stuck.example"

            def crawl(self, limit=None):
                _time.sleep(0.02)

        before = self.conn.execute("SELECT count(*) AS n FROM crawl_jobs").fetchone()["n"]
        jobs.enqueue_job(self.conn, "rc-backfill-stuck", mode="backfill")
        job = jobs.claim_next_job(self.conn)
        with mock.patch.dict(os.environ, {"LIBERTREE_MAX_WALL_S": "0.01"}):
            worker.run_job(self.conn, job, crawler_registry={"rc-backfill-stuck": StuckBackfillCrawler}, delay=0)
        after = self.conn.execute("SELECT count(*) AS n FROM crawl_jobs").fetchone()["n"]
        self.assertEqual(after, before + 1)  # 재큐잉 없음 (원 작업 1건뿐)

    def test_completed_backfill_marks_done_and_stops_chain(self):
        from crawler.base_crawler import BaseCrawler
        from delivery.worker import jobs, worker

        class DoneBackfillCrawler(BaseCrawler):
            site_id = "rc-backfill-done"
            site_name = "RC Backfill Done"
            base_url = "https://rc-backfill-done.example"

            def crawl(self, limit=None):
                self._advance_cursor({"page": 9})

        jobs.enqueue_job(self.conn, "rc-backfill-done", mode="backfill")
        job = jobs.claim_next_job(self.conn)
        worker.run_job(self.conn, job, crawler_registry={"rc-backfill-done": DoneBackfillCrawler}, delay=0)
        row = self.conn.execute(
            "SELECT completed_at, cursor FROM crawl_site_progress WHERE site_id=%s",
            ("rc-backfill-done",)).fetchone()
        self.assertIsNotNone(row["completed_at"])
        self.assertIsNotNone(row["cursor"])     # 커서는 지우지 않는다
        tail = self.conn.execute(
            "SELECT count(*) AS n FROM crawl_jobs WHERE site_id=%s AND status='queued'",
            ("rc-backfill-done",)).fetchone()
        self.assertEqual(tail["n"], 0)

    def test_cancelled_backfill_saves_cursor_but_breaks_chain(self):
        # 기존 취소 테스트 픽스처(jobs.cancel_job + should_cancel + _check_cancelled)를
        # 그대로 재사용한다: 취소 경로에서 커서는 저장, 재큐잉은 없음.
        from crawler.base_crawler import BaseCrawler
        from delivery.worker import jobs, worker

        class CancellableBackfillCrawler(BaseCrawler):
            site_id = "rc-backfill-cancel"
            site_name = "RC Backfill Cancel"
            base_url = "https://rc-backfill-cancel.example"

            def crawl(self, limit=None):
                self._advance_cursor({"page": 2})
                self._check_cancelled()

        jid = jobs.enqueue_job(self.conn, "rc-backfill-cancel", mode="backfill")
        job = jobs.claim_next_job(self.conn)
        jobs.cancel_job(self.conn, jid)
        worker.run_job(self.conn, job, {"rc-backfill-cancel": CancellableBackfillCrawler},
                       delay=0, should_cancel=lambda: True)
        row = self.conn.execute("SELECT status FROM crawl_jobs WHERE id=%s", (jid,)).fetchone()
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(jobs.load_cursor(self.conn, "rc-backfill-cancel"), {"page": 2})
        tail = self.conn.execute(
            "SELECT count(*) AS n FROM crawl_jobs WHERE site_id=%s AND status='queued'",
            ("rc-backfill-cancel",)).fetchone()
        self.assertEqual(tail["n"], 0)

    def test_failed_backfill_keeps_previous_cursor(self):
        from crawler.base_crawler import BaseCrawler
        from delivery.worker import jobs, worker

        class BoomBackfillCrawler(BaseCrawler):
            site_id = "rc-backfill-boom"
            site_name = "RC Backfill Boom"
            base_url = "https://rc-backfill-boom.example"

            def crawl(self, limit=None):
                raise RuntimeError("boom")

        jobs.save_progress(self.conn, "rc-backfill-boom", {"page": 7})
        jobs.enqueue_job(self.conn, "rc-backfill-boom", mode="backfill")
        job = jobs.claim_next_job(self.conn)
        worker.run_job(self.conn, job, crawler_registry={"rc-backfill-boom": BoomBackfillCrawler}, delay=0)
        self.assertEqual(jobs.load_cursor(self.conn, "rc-backfill-boom"), {"page": 7})

    def test_up_to_date_is_finished_as_plain_done(self):
        from crawler.base_crawler import BaseCrawler, CrawlUpToDate
        from delivery.worker import jobs, worker

        class UpToDateNowCrawler(BaseCrawler):
            site_id = "rc-uptodate2"
            site_name = "RC UpToDate 2"
            base_url = "https://rc-uptodate2.example"

            def crawl(self, limit=None):
                raise CrawlUpToDate("already have everything")

        jid = jobs.enqueue_job(self.conn, "rc-uptodate2", mode="incremental")
        job = jobs.claim_next_job(self.conn)
        worker.run_job(self.conn, job, crawler_registry={"rc-uptodate2": UpToDateNowCrawler}, delay=0)
        row = self.conn.execute(
            "SELECT status, truncated FROM crawl_jobs WHERE id=%s", (jid,)).fetchone()
        self.assertEqual((row["status"], row["truncated"]), ("done", False))

    def test_oldest_first_incremental_gets_cursor_and_persists_advance(self):
        # DELIVERY_ORDER="oldest_first" 는 incremental 이어도 커서를 받는다.
        # 단, incremental 은 잘려도 재큐잉하지 않는다 (backfill 전용).
        import time as _time
        from unittest import mock
        from crawler.base_crawler import BaseCrawler
        from delivery.worker import jobs, worker

        class OldestFirstCrawler(BaseCrawler):
            site_id = "rc-oldest"
            site_name = "RC Oldest"
            base_url = "https://rc-oldest.example"
            DELIVERY_ORDER = "oldest_first"

            def crawl(self, limit=None):
                _time.sleep(0.02)
                offset = self.delivery_cursor["offset"]
                self._advance_cursor({"offset": offset + 60})

        jobs.save_progress(self.conn, "rc-oldest", {"offset": 60})
        jobs.enqueue_job(self.conn, "rc-oldest", mode="incremental")
        job = jobs.claim_next_job(self.conn)
        with mock.patch.dict(os.environ, {"LIBERTREE_MAX_WALL_S": "0.01"}):
            worker.run_job(self.conn, job, crawler_registry={"rc-oldest": OldestFirstCrawler}, delay=0)
        self.assertEqual(jobs.load_cursor(self.conn, "rc-oldest"), {"offset": 120})
        tail = self.conn.execute(
            "SELECT count(*) AS n FROM crawl_jobs WHERE site_id=%s", ("rc-oldest",)).fetchone()
        self.assertEqual(tail["n"], 1)  # 잘렸어도 incremental 은 재큐잉하지 않는다


if __name__ == "__main__":
    unittest.main()
