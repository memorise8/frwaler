"""Incremental mode wiring: collect runner admission/exec, schedules, BE check (no network, no PG, no docker)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

LPB = {"strategy": "last_page_backward", "listing": {"url": "https://board.test/list"}}


def load_runner(home):
    os.environ["RUNNER_HOME"] = home
    spec = importlib.util.spec_from_file_location("collect_runner_under_test",
                                                  os.path.join(ROOT, "deploy", "collect-runner", "runner.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RunnerIncrementalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        old = os.environ.get("RUNNER_HOME")
        self.addCleanup(lambda: os.environ.__setitem__("RUNNER_HOME", old) if old else os.environ.pop("RUNNER_HOME", None))
        self.r = load_runner(self.home)
        self.r.docker_running = lambda: set()
        os.makedirs(os.path.join(self.home, "logs"))
        self.c = self.r.db()
        self.reg = {}

    def register(self, site_id, site, *, ready=True, expected=None, approved=False):
        plan_file = os.path.join(self.home, site_id + ".json")
        with open(plan_file, "w") as fh:
            json.dump({"sites": {site_id: site}}, fh)
        self.reg[site_id] = {"site_id": site_id, "plan_file": plan_file, "unit": site_id + ".test",
                             "sha256": hashlib.sha256(open(plan_file, "rb").read()).hexdigest(), "src": ROOT,
                             "ready": ready, "why": None if ready else "not yet", "expected_items": expected,
                             "approved_large": approved, "strategy": site.get("strategy")}
        self.r.save_registry(self.reg)

    def test_incremental_needs_no_large_approval_but_a_ready_newest_first_plan(self):
        self.register("big", LPB)  # expected unknown, no --approved-large
        self.assertEqual(self.r.check_request(self.c, "big", "resume", None)[0], "large_needs_approval")
        self.assertEqual(self.r.check_request(self.c, "big", "incremental", None), (None, None))
        self.assertEqual(self.r.check_request(self.c, "big", "incremental", 10)[0], "bad_limit")
        self.register("notready", LPB, ready=False)
        self.assertEqual(self.r.check_request(self.c, "notready", "incremental", None)[0], "plan_not_ready")
        self.register("oldest", {"strategy": "oldest_first_sort", "listing": {"url": "https://o.test/"}})
        self.assertEqual(self.r.check_request(self.c, "oldest", "incremental", None)[0], "incremental_unsupported")
        self.register("sitemap", {"strategy": "sitemap"})
        self.assertEqual(self.r.check_request(self.c, "sitemap", "incremental", None)[0], "incremental_unsupported")
        self.register("unknown", dict(LPB, incremental={"order": "unknown"}))
        self.assertEqual(self.r.check_request(self.c, "unknown", "incremental", None)[0], "incremental_unsupported")
        self.assertIsNone(self.r.incremental_refusal(self.reg["big"]))
        self.assertEqual(self.r.incremental_refusal(self.reg["oldest"]), "listing_order_oldest_first")

    def test_incremental_is_one_site_and_one_unit_at_a_time(self):
        self.register("a", LPB)
        self.c.execute("insert into runs(run_id,site_id,mode,status,unit) values('r0001','a','incremental','running','a.test')")
        self.assertEqual(self.r.check_request(self.c, "a", "incremental", None)[0], "site_busy")
        self.register("b", LPB)
        self.reg["b"]["unit"] = "a.test"
        self.r.save_registry(self.reg)
        self.assertEqual(self.r.check_request(self.c, "b", "incremental", None)[0], "unit_busy")

    def test_exec_runs_in_the_resume_folder_with_incremental_env(self):
        self.register("a", LPB)
        kk_dir = os.path.join(self.home, "known")
        os.makedirs(kk_dir)
        open(os.path.join(kk_dir, "a.csv"), "w").write("post_number,meta_url\n")
        out = os.path.join(self.home, "env.json")
        cfg = {"fake_cmd": [sys.executable, "-c", "import json,os,sys; json.dump({k: os.environ.get(k) for k in "
                            "('ENUM_INCREMENTAL','ENUM_KNOWN_KEYS','ENUM_PILOT_DATE','ENUM_EXTRA_ARGS')}, open(sys.argv[1],'w'))",
                            out], "known_keys_dir": kk_dir, "runs_root": self.home}
        json.dump(cfg, open(os.path.join(self.home, "config.json"), "w"))
        self.c.execute("insert into site_folder values('a','20261001-s1','x')")
        self.c.execute("insert into runs(run_id,site_id,mode,status,unit) values('r0001','a','incremental','queued','a.test')")
        with mock.patch.object(self.r.subprocess, "Popen") as popen:
            popen.return_value.pid = 4242
            self.r.tick()
        row = self.c.execute("select * from runs where run_id='r0001'").fetchone()
        self.assertEqual((row["status"], row["tag"]), ("running", "20261001-s1"))  # the site's own folder
        with mock.patch.dict(os.environ, {"ENUM_EXTRA_ARGS": "--refresh"}):
            self.assertEqual(self.r._exec(self.c, "r0001"), 0)
        env = json.load(open(out))
        self.assertEqual(env, {"ENUM_INCREMENTAL": "1", "ENUM_KNOWN_KEYS": os.path.join(kk_dir, "a.csv"),
                               "ENUM_PILOT_DATE": "20261001-s1", "ENUM_EXTRA_ARGS": None})

    def test_resume_exec_never_inherits_incremental_env(self):
        self.register("a", LPB, approved=True)
        out = os.path.join(self.home, "env.json")
        json.dump({"fake_cmd": [sys.executable, "-c", "import json,os,sys; json.dump(os.environ.get('ENUM_INCREMENTAL'),"
                                " open(sys.argv[1],'w'))", out]}, open(os.path.join(self.home, "config.json"), "w"))
        self.c.execute("insert into runs(run_id,site_id,mode,status,unit,tag) values('r0002','a','resume','running','a.test','20261001-s1')")
        with mock.patch.dict(os.environ, {"ENUM_INCREMENTAL": "1"}):
            self.assertEqual(self.r._exec(self.c, "r0002"), 0)
        self.assertIsNone(json.load(open(out)))


class FakeCursor:
    def __init__(self, rows=None):
        self.rows = rows or []

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConn:
    def __init__(self, schedules):
        self.schedules = schedules
        self.sql = []

    def execute(self, sql, args=()):
        self.sql.append((" ".join(sql.split()), args))
        if sql.lstrip().startswith("SELECT * FROM crawl_schedules"):
            return FakeCursor(self.schedules)
        if "RETURNING id" in sql:
            return FakeCursor([{"id": 99}])
        return FakeCursor([])

    def commit(self):
        self.sql.append(("COMMIT", ()))


class ScheduleRoutingTest(unittest.TestCase):
    def test_upsert_validates_enum_incremental(self):
        from delivery.worker import schedules
        with self.assertRaises(ValueError):
            schedules.upsert(None, site_id="s", interval_hours=24, mode="enum_incremental", limit_n=100)
        with self.assertRaises(ValueError):
            schedules.upsert(None, site_id="s", interval_hours=24, mode="backfill")
        conn = FakeConn([])
        conn.execute = mock.Mock(return_value=FakeCursor([{"site_id": "s", "mode": "enum_incremental"}]))
        self.assertEqual(schedules.upsert(conn, site_id="s", interval_hours=24, mode="enum_incremental", limit_n=None)["mode"],
                         "enum_incremental")

    def test_enum_incremental_goes_to_the_runner_not_crawl_jobs(self):
        from delivery.worker import schedules
        conn = FakeConn([{"id": 1, "site_id": "enum-site", "mode": "enum_incremental", "limit_n": None},
                         {"id": 2, "site_id": "legacy", "mode": "incremental", "limit_n": 5}])
        sent = []
        created = schedules.enqueue_due(conn, lambda site, mode: (sent.append((site, mode)) or (201, {"run_id": "r0009"})))
        self.assertEqual(created, 2)
        self.assertEqual(sent, [("enum-site", "incremental")])
        jobs = [args for sql, args in conn.sql if sql.startswith("INSERT INTO crawl_jobs")]
        self.assertEqual(jobs, [("legacy", "incremental", 5)])
        advanced = [args for sql, args in conn.sql if sql.startswith("UPDATE crawl_schedules")]
        self.assertEqual(advanced, [(1,), (2,)])

    def test_runner_is_called_only_after_the_claim_is_committed(self):
        from delivery.worker import schedules
        conn = FakeConn([{"id": 1, "site_id": "a", "mode": "enum_incremental", "limit_n": None},
                         {"id": 2, "site_id": "b", "mode": "enum_incremental", "limit_n": None}])
        seen = []

        def submit(site, mode):
            done = [s for s, _ in conn.sql]
            last_commit = max(i for i, s in enumerate(done) if s == "COMMIT")
            claimed = [i for i, s in enumerate(done) if s.startswith("UPDATE crawl_schedules SET last_run_at")]
            seen.append((site, all(i < last_commit for i in claimed), len(claimed)))
            return 201, {"run_id": "r1"}

        self.assertEqual(schedules.enqueue_due(conn, submit), 2)
        self.assertEqual(seen, [("a", True, 2), ("b", True, 2)])

    def test_timeout_is_logged_unknown_not_resubmitted_and_rest_stay_due(self):
        import io
        from contextlib import redirect_stdout
        from delivery.worker import schedules
        conn = FakeConn([{"id": 1, "site_id": "a", "mode": "enum_incremental", "limit_n": None},
                         {"id": 2, "site_id": "b", "mode": "enum_incremental", "limit_n": None},
                         {"id": 3, "site_id": "c", "mode": "enum_incremental", "limit_n": None}])
        calls = []

        def submit(site, mode):
            calls.append(site)
            raise TimeoutError("timed out")

        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(schedules.enqueue_due(conn, submit), 0)
        self.assertEqual(calls, ["a"])  # one timeout per cycle, not N
        lines = [json.loads(l) for l in out.getvalue().splitlines()]
        self.assertEqual(lines[0], {"event": "schedule_runner_request", "site_id": "a", "outcome": "unknown",
                                    "error": "TimeoutError"})
        self.assertEqual(lines[1]["outcome"], "deferred_runner_unreachable")
        redue = [args for sql, args in conn.sql if sql.startswith("UPDATE crawl_schedules SET next_run_at=now()")]
        self.assertEqual(redue, [(2,), (3,)])  # "a" keeps its advanced time: never double-started

    def test_runner_site_busy_is_recorded_as_refused(self):
        import io
        from contextlib import redirect_stdout
        from delivery.worker import schedules
        conn = FakeConn([{"id": 1, "site_id": "a", "mode": "enum_incremental", "limit_n": None}])
        out = io.StringIO()
        with redirect_stdout(out):
            schedules.enqueue_due(conn, lambda s, m: (409, {"reason": "site_busy", "detail": "x"}))
        line = json.loads(out.getvalue().splitlines()[0])
        self.assertEqual((line["outcome"], line["status"], line["reason"]), ("refused", 409, "site_busy"))

    def test_without_runner_access_enum_schedules_stay_due(self):
        from delivery.worker import schedules
        conn = FakeConn([{"id": 1, "site_id": "enum-site", "mode": "enum_incremental", "limit_n": None}])
        self.assertEqual(schedules.enqueue_due(conn), 0)
        self.assertFalse([s for s, _ in conn.sql if s.startswith(("UPDATE crawl_schedules", "INSERT INTO crawl_jobs"))])

    def test_runner_refusal_waits_for_the_next_interval(self):
        from delivery.worker import schedules
        conn = FakeConn([{"id": 1, "site_id": "enum-site", "mode": "enum_incremental", "limit_n": None}])
        self.assertEqual(schedules.enqueue_due(conn, lambda s, m: (409, {"reason": "site_busy"})), 0)
        self.assertTrue([s for s, _ in conn.sql if s.startswith("UPDATE crawl_schedules")])

    def test_worker_submits_only_with_explicit_runner_access(self):
        from delivery.worker import worker
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COLLECT_RUNNER_SOCKET", None)
            os.environ.pop("COLLECT_RUNNER_URL", None)
            self.assertIsNone(worker.schedule_runner_submit())
        with mock.patch.dict(os.environ, {"COLLECT_RUNNER_URL": "http://runner:8793"}), \
                mock.patch("delivery.be.collect.runner_call", return_value=(201, {"run_id": "r0001"})) as call:
            submit = worker.schedule_runner_submit()
            self.assertEqual(submit("s", "incremental"), (201, {"run_id": "r0001"}))
            call.assert_called_once_with("POST", "/runner/runs", {"site_id": "s", "mode": "incremental",
                                                                  "requested_by": "scheduler"})

    @unittest.skipUnless(importlib.util.find_spec("psycopg_pool") and importlib.util.find_spec("fastapi"),
                         "delivery.be.app needs psycopg_pool and fastapi")
    def test_be_requires_a_ready_newest_first_runner_plan(self):
        from fastapi import HTTPException
        from delivery.be import app
        plans = [{"site_id": "ok", "ready": True, "incremental_refusal": None},
                 {"site_id": "old", "ready": True, "incremental_refusal": "listing_order_oldest_first"},
                 {"site_id": "nr", "ready": False, "why": "x", "incremental_refusal": None},
                 {"site_id": "legacy-runner", "ready": True}]
        with mock.patch("delivery.be.collect.runner_call", return_value=(200, plans)):
            app._require_runner_incremental("ok")
            for site in ("old", "nr", "missing", "legacy-runner"):
                with self.subTest(site=site), self.assertRaises(HTTPException) as ctx:
                    app._require_runner_incremental(site)
                self.assertEqual(ctx.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
