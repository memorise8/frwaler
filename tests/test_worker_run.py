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
        for t in ("crawl_job_logs", "crawl_jobs", "crawl_site_progress", "documents", "sites"):
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
                    self._advance_cursor({"page": page}, items_done=1)

        jobs.enqueue_job(self.conn, "rc-backfill-a", mode="backfill")
        job = jobs.claim_next_job(self.conn)
        with mock.patch.dict(os.environ, {"LIBERTREE_MAX_WALL_S": "0.01"}):
            worker.run_job(self.conn, job, crawler_registry={"rc-backfill-a": PagedBackfillCrawler}, delay=0)
        cur = jobs.load_cursor(self.conn, "rc-backfill-a")
        self.assertEqual(cur, {"page": 3})
        queued = self.conn.execute(
            "SELECT site_id, mode, status, requested_by FROM crawl_jobs"
            " WHERE site_id=%s AND status='queued'", ("rc-backfill-a",)
        ).fetchall()
        self.assertEqual(len(queued), 1)  # 정확히 한 건만 재큐잉된다
        self.assertEqual(
            (queued[0]["site_id"], queued[0]["mode"], queued[0]["status"], queued[0]["requested_by"]),
            ("rc-backfill-a", "backfill", "queued", "auto-backfill"))

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
        jid = jobs.enqueue_job(self.conn, "rc-backfill-stuck", mode="backfill")
        job = jobs.claim_next_job(self.conn)
        with mock.patch.dict(os.environ, {"LIBERTREE_MAX_WALL_S": "0.01"}):
            worker.run_job(self.conn, job, crawler_registry={"rc-backfill-stuck": StuckBackfillCrawler}, delay=0)
        after = self.conn.execute("SELECT count(*) AS n FROM crawl_jobs").fetchone()["n"]
        self.assertEqual(after, before + 1)  # 재큐잉 없음 (원 작업 1건뿐)
        stalled = self.conn.execute(
            "SELECT 1 FROM crawl_job_logs WHERE job_id=%s AND event='stalled'", (jid,)).fetchone()
        self.assertIsNotNone(stalled)

    def test_completed_backfill_marks_done_and_stops_chain(self):
        # 완주는 이제 시간·예산이 아니라 크롤러의 명시 신호(_mark_exhausted)로만
        # 판정한다 -- 이 신호 없이는 아무리 조용히 정상 반환해도 완주가 아니다.
        from crawler.base_crawler import BaseCrawler
        from delivery.worker import jobs, worker

        class DoneBackfillCrawler(BaseCrawler):
            site_id = "rc-backfill-done"
            site_name = "RC Backfill Done"
            base_url = "https://rc-backfill-done.example"

            def crawl(self, limit=None):
                self._advance_cursor({"page": 9})
                self._mark_exhausted()

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

    # --- fix round 1: state machine hardening ---------------------------

    def test_non_backfill_orders_never_touch_saved_backfill_progress(self):
        # full 모드와 newest_first incremental 은 커서를 아예 만지지 않는다 --
        # 가짜 크롤러가 _advance_cursor 를 불러도 저장된 백필 진도는 그대로다.
        from crawler.base_crawler import BaseCrawler
        from delivery.worker import jobs, worker

        class TouchyFullCrawler(BaseCrawler):
            site_id = "rc-full-touch"
            site_name = "RC Full Touch"
            base_url = "https://rc-full-touch.example"

            def crawl(self, limit=None):
                self._advance_cursor({"page": 2}, items_done=5)

        class TouchyNewestFirstCrawler(BaseCrawler):
            site_id = "rc-newest-touch"
            site_name = "RC Newest Touch"
            base_url = "https://rc-newest-touch.example"
            DELIVERY_ORDER = "newest_first"

            def crawl(self, limit=None):
                self._advance_cursor({"page": 2}, items_done=5)

        jobs.save_progress(self.conn, "rc-full-touch", {"page": 300})
        jobs.enqueue_job(self.conn, "rc-full-touch", mode="full")
        worker.run_job(self.conn, jobs.claim_next_job(self.conn),
                       crawler_registry={"rc-full-touch": TouchyFullCrawler}, delay=0)
        self.assertEqual(jobs.load_cursor(self.conn, "rc-full-touch"), {"page": 300})

        jobs.save_progress(self.conn, "rc-newest-touch", {"page": 300})
        jobs.enqueue_job(self.conn, "rc-newest-touch", mode="incremental")
        worker.run_job(self.conn, jobs.claim_next_job(self.conn),
                       crawler_registry={"rc-newest-touch": TouchyNewestFirstCrawler}, delay=0)
        self.assertEqual(jobs.load_cursor(self.conn, "rc-newest-touch"), {"page": 300})

    def test_backfill_up_to_date_persists_advance_and_completes(self):
        # backfill 이 완주 대신 CrawlUpToDate 를 만나도 완료 신호다: 커서는
        # 저장하고 completed_at 도 찍는다.
        from crawler.base_crawler import BaseCrawler, CrawlUpToDate
        from delivery.worker import jobs, worker

        class UpToDateBackfillCrawler(BaseCrawler):
            site_id = "rc-backfill-uptodate"
            site_name = "RC Backfill UpToDate"
            base_url = "https://rc-backfill-uptodate.example"

            def crawl(self, limit=None):
                self._advance_cursor({"page": 42}, items_done=3)
                raise CrawlUpToDate("already have everything")

        jid = jobs.enqueue_job(self.conn, "rc-backfill-uptodate", mode="backfill")
        job = jobs.claim_next_job(self.conn)
        worker.run_job(self.conn, job,
                       crawler_registry={"rc-backfill-uptodate": UpToDateBackfillCrawler}, delay=0)
        self.assertEqual(jobs.load_cursor(self.conn, "rc-backfill-uptodate"), {"page": 42})
        row = self.conn.execute(
            "SELECT completed_at FROM crawl_site_progress WHERE site_id=%s",
            ("rc-backfill-uptodate",)).fetchone()
        self.assertIsNotNone(row["completed_at"])
        jrow = self.conn.execute(
            "SELECT status, truncated FROM crawl_jobs WHERE id=%s", (jid,)).fetchone()
        self.assertEqual((jrow["status"], jrow["truncated"]), ("done", False))

    def test_requeue_collision_logs_and_does_not_crash(self):
        # 경쟁 행이 이미 큐에 있으면 enqueue_job 이 ActiveJobError 를 던진다 --
        # 워커는 죽지 않고, 재큐잉을 건너뛴 로그만 남긴다.
        import time as _time
        from unittest import mock
        from crawler.base_crawler import BaseCrawler
        from delivery.worker import jobs, worker

        class PagedBackfillCrawler(BaseCrawler):
            site_id = "rc-backfill-collide"
            site_name = "RC Backfill Collide"
            base_url = "https://rc-backfill-collide.example"

            def crawl(self, limit=None):
                _time.sleep(0.02)
                self._advance_cursor({"page": 2}, items_done=1)

        jid = jobs.enqueue_job(self.conn, "rc-backfill-collide", mode="backfill")
        job = jobs.claim_next_job(self.conn)
        # 원 작업을 claim 한 뒤, 같은 사이트에 다른 경로(수동 요청 등)로 이미
        # queued 행이 하나 더 생겼다고 가정한다.
        self.conn.execute(
            "INSERT INTO crawl_jobs(site_id, mode, status) VALUES(%s,'backfill','queued')",
            ("rc-backfill-collide",))
        self.conn.commit()
        with mock.patch.dict(os.environ, {"LIBERTREE_MAX_WALL_S": "0.01"}):
            worker.run_job(self.conn, job,
                           crawler_registry={"rc-backfill-collide": PagedBackfillCrawler}, delay=0)
        queued = self.conn.execute(
            "SELECT count(*) AS n FROM crawl_jobs WHERE site_id=%s AND status='queued'",
            ("rc-backfill-collide",)).fetchone()
        self.assertEqual(queued["n"], 1)  # 중복 재큐잉 없음 -- 기존 경쟁 행 그대로
        skipped = self.conn.execute(
            "SELECT 1 FROM crawl_job_logs WHERE job_id=%s AND event='requeue_skipped'",
            (jid,)).fetchone()
        self.assertIsNotNone(skipped)

    def test_backfill_with_no_progress_is_not_marked_complete(self):
        # 아무 것도 걷지 못한 채(커서 보고도, 저장도 없이, exhausted 신호도 없이)
        # 정상 반환한 backfill 은 완주로 표시하면 안 된다 -- 재시도할 방법이
        # 사라진다. 전진도 exhausted 도 없으니 재큐잉도 하지 않고 stalled 로 남는다.
        from crawler.base_crawler import BaseCrawler
        from delivery.worker import jobs, worker

        class NoOpBackfillCrawler(BaseCrawler):
            site_id = "rc-backfill-noop"
            site_name = "RC Backfill NoOp"
            base_url = "https://rc-backfill-noop.example"

            def crawl(self, limit=None):
                pass

        jid = jobs.enqueue_job(self.conn, "rc-backfill-noop", mode="backfill")
        job = jobs.claim_next_job(self.conn)
        worker.run_job(self.conn, job,
                       crawler_registry={"rc-backfill-noop": NoOpBackfillCrawler}, delay=0)
        row = self.conn.execute(
            "SELECT completed_at FROM crawl_site_progress WHERE site_id=%s",
            ("rc-backfill-noop",)).fetchone()
        self.assertIsNone(row)  # progress row 자체가 생기지 않았다
        stalled = self.conn.execute(
            "SELECT 1 FROM crawl_job_logs WHERE job_id=%s AND event='stalled'",
            (jid,)).fetchone()
        self.assertIsNotNone(stalled)

    # --- final fix round: completion requires the explicit exhausted signal -

    def test_fast_chunk_without_exhausted_signal_requeues_not_completes(self):
        # CRITICAL: doaj 의 200페이지 조각(~16분)은 25분 예산의 truncation 임계
        # 아래라 "안 잘렸다"로 보인다. 시간으로 완주를 추론하던 예전 로직은
        # 이 조각을 첫 실행에서 곧바로 완주 처리해 13,373,055 건 중 만 건도
        # 못 걸은 채 체인이 죽었다. exhausted 신호가 없으면, 잘리지 않고 빨리
        # 돌아와도 완주가 아니라 재큐잉 대상이어야 한다.
        from crawler.base_crawler import BaseCrawler
        from delivery.worker import jobs, worker

        class FastChunkCrawler(BaseCrawler):
            site_id = "rc-backfill-fastchunk"
            site_name = "RC Backfill Fast Chunk"
            base_url = "https://rc-backfill-fastchunk.example"

            def crawl(self, limit=None):
                # 예산 소진으로 조기 반환하는 조각을 흉내낸다 -- 빠르게 끝나고
                # (안 잘림) _mark_exhausted 는 부르지 않는다.
                self._advance_cursor({"page": 201}, items_done=200)

        jobs.enqueue_job(self.conn, "rc-backfill-fastchunk", mode="backfill")
        job = jobs.claim_next_job(self.conn)
        worker.run_job(self.conn, job,
                       crawler_registry={"rc-backfill-fastchunk": FastChunkCrawler}, delay=0)
        row = self.conn.execute(
            "SELECT completed_at FROM crawl_site_progress WHERE site_id=%s",
            ("rc-backfill-fastchunk",)).fetchone()
        self.assertIsNone(row["completed_at"])
        queued = self.conn.execute(
            "SELECT count(*) AS n FROM crawl_jobs WHERE site_id=%s AND status='queued'",
            ("rc-backfill-fastchunk",)).fetchone()
        self.assertEqual(queued["n"], 1)

    def test_fetch_failure_shaped_return_requeues_not_completes(self):
        # 여러 페이지를 걷다가(전진 보고 있음) fetch 실패로 조용히 반환한 조각도
        # exhausted 신호가 없으니 완주가 아니다 -- 재큐잉된다.
        from crawler.base_crawler import BaseCrawler
        from delivery.worker import jobs, worker

        class FetchFailureCrawler(BaseCrawler):
            site_id = "rc-backfill-fetchfail"
            site_name = "RC Backfill Fetch Failure"
            base_url = "https://rc-backfill-fetchfail.example"

            def crawl(self, limit=None):
                page = (self.delivery_cursor or {}).get("page", 1)
                for _ in range(3):
                    page += 1
                    self._advance_cursor({"page": page}, items_done=10)
                # fetch 실패를 흉내내며 exhausted 없이 조용히 반환한다.

        jobs.enqueue_job(self.conn, "rc-backfill-fetchfail", mode="backfill")
        job = jobs.claim_next_job(self.conn)
        worker.run_job(self.conn, job,
                       crawler_registry={"rc-backfill-fetchfail": FetchFailureCrawler}, delay=0)
        row = self.conn.execute(
            "SELECT completed_at, cursor FROM crawl_site_progress WHERE site_id=%s",
            ("rc-backfill-fetchfail",)).fetchone()
        self.assertIsNone(row["completed_at"])
        self.assertEqual(row["cursor"], {"page": 4})
        queued = self.conn.execute(
            "SELECT count(*) AS n FROM crawl_jobs WHERE site_id=%s AND status='queued'",
            ("rc-backfill-fetchfail",)).fetchone()
        self.assertEqual(queued["n"], 1)

    def test_cancel_during_normal_return_backfill_breaks_chain(self):
        # HIGH: 취소가 정상 반환과 동시에 도착하면(finish_job 의 running->done
        # UPDATE 가 더는 매치하지 않고 cancelling->cancelled 로 떨어지는 경우)
        # 커서는 저장하되 완주·재큐잉은 절대 하지 않는다 -- 운영자의 취소가
        # 조용히 무시되면 안 된다.
        from crawler.base_crawler import BaseCrawler
        from delivery.worker import jobs, worker

        class NormalReturnCrawler(BaseCrawler):
            site_id = "rc-backfill-late-cancel"
            site_name = "RC Backfill Late Cancel"
            base_url = "https://rc-backfill-late-cancel.example"

            def crawl(self, limit=None):
                # 취소를 확인하지 않고 정상 반환한다 -- 취소가 마지막 안전
                # 경계 이후에 도착한 경우를 흉내낸다.
                self._advance_cursor({"page": 2}, items_done=5)

        jid = jobs.enqueue_job(self.conn, "rc-backfill-late-cancel", mode="backfill")
        job = jobs.claim_next_job(self.conn)
        jobs.cancel_job(self.conn, jid)  # running -> cancelling
        worker.run_job(self.conn, job,
                       crawler_registry={"rc-backfill-late-cancel": NormalReturnCrawler}, delay=0)
        row = self.conn.execute(
            "SELECT status FROM crawl_jobs WHERE id=%s", (jid,)).fetchone()
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(jobs.load_cursor(self.conn, "rc-backfill-late-cancel"), {"page": 2})
        progress = self.conn.execute(
            "SELECT completed_at FROM crawl_site_progress WHERE site_id=%s",
            ("rc-backfill-late-cancel",)).fetchone()
        self.assertIsNone(progress["completed_at"])
        queued = self.conn.execute(
            "SELECT count(*) AS n FROM crawl_jobs WHERE site_id=%s AND status='queued'",
            ("rc-backfill-late-cancel",)).fetchone()
        self.assertEqual(queued["n"], 0)


if __name__ == "__main__":
    unittest.main()
