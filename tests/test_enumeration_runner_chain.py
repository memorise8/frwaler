"""Collect runner: resume options reprocess_failed / auto_continue (no network, no docker)."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LPB = {"strategy": "last_page_backward", "listing": {"url": "https://board.test/list"}}
TIME_BUDGET = {"site_status": "incomplete", "stop_reason": "time_budget", "saved": 10, "failed": 0}


def load_runner(home):
    os.environ["RUNNER_HOME"] = home
    spec = importlib.util.spec_from_file_location("collect_runner_chain_under_test",
                                                  os.path.join(ROOT, "deploy", "collect-runner", "runner.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RunnerChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        old = os.environ.get("RUNNER_HOME")
        self.addCleanup(lambda: os.environ.__setitem__("RUNNER_HOME", old) if old else os.environ.pop("RUNNER_HOME", None))
        self.r = load_runner(self.home)
        self.r.docker_running = lambda: set()
        os.makedirs(os.path.join(self.home, "logs"))
        open(os.path.join(self.home, ".token"), "w").write("tok")
        self.c = self.r.db()
        self.reg = {}
        self.result = dict(TIME_BUDGET)
        self.r.read_result = lambda tag, site_id: dict(self.result)

    def register(self, site_id, *, ready=True, expected=None, approved=True, unit=None):
        plan_file = os.path.join(self.home, site_id + ".json")
        with open(plan_file, "w") as fh:
            json.dump({"sites": {site_id: LPB}}, fh)
        self.reg[site_id] = {"site_id": site_id, "plan_file": plan_file, "unit": unit or site_id + ".test",
                             "sha256": hashlib.sha256(open(plan_file, "rb").read()).hexdigest(), "src": ROOT,
                             "ready": ready, "why": None if ready else "not yet", "expected_items": expected,
                             "approved_large": approved, "strategy": "last_page_backward"}
        self.r.save_registry(self.reg)

    def post(self, body):
        raw = json.dumps(body).encode()
        h = self.r.H.__new__(self.r.H)
        h.headers = {"X-Runner-Token": "tok", "Content-Length": str(len(raw))}
        h.rfile, h.path, out = io.BytesIO(raw), "/runner/runs", []
        h.reply = lambda code, obj: out.append((code, obj))
        h.do_POST()
        return out[0]

    def row(self, run_id):
        return self.c.execute("select * from runs where run_id=?", (run_id,)).fetchone()

    def start(self, run_id):
        self.c.execute("update runs set status='running', tag=coalesce(tag,'20261001-s1') where run_id=?", (run_id,))
        return self.row(run_id)

    def finish(self, run_id, rc=0):
        self.r.finish(self.c, self.start(run_id), rc)

    def events(self, action):
        return [json.loads(e["detail"] or "null") for e in
                self.c.execute("select detail from events where action=? order by id", (action,))]

    # -- validation / pass-through ------------------------------------------------
    def test_reprocess_failed_validation(self):
        ok = self.r.check_options
        self.assertEqual(ok("resume", {"reprocess_failed": ["empty_item", "parse_error:", "empty_item"]}),
                         (None, {"reprocess_failed": ["empty_item", "parse_error:"]}))
        self.assertEqual(ok("resume", {"reprocess_failed": ["adapter_contract:paper_title_missing", "transient:http_503",
                                                            "save_transient:OperationalError", "save_error"]})[0], None)
        for bad in (["identity_collision"], ["listing_identity_conflict"], ["skipped_by_rule"], ["http_404"],
                    ["empty_item --refresh"], ["empty_item;rm"], ["parse_error:*"], ["../x"], [""], [], "empty_item",
                    [5], ["empty_item"] * 7, ["x" * 81]):
            self.assertEqual(ok("resume", {"reprocess_failed": bad})[0], "bad_reprocess_failed", bad)
        for mode in ("trial", "incremental"):
            self.assertEqual(ok(mode, {"reprocess_failed": ["empty_item"]})[0], "bad_reprocess_failed")

    def test_auto_continue_validation(self):
        ok = self.r.check_options
        self.assertEqual(ok("resume", {"auto_continue": {}}), (None, {"auto_continue": {"max_runs": 5, "on": ["time_budget"]}}))
        self.assertEqual(ok("resume", {"auto_continue": {"max_runs": 20}})[1]["auto_continue"]["max_runs"], 20)
        for bad in ({"max_runs": 0}, {"max_runs": 21}, {"max_runs": True}, {"max_runs": "5"}, {"on": ["complete"]},
                    {"on": []}, {"on": "time_budget"}, {"other": 1}, True, [1]):
            self.assertEqual(ok("resume", {"auto_continue": bad})[0], "bad_auto_continue", bad)
        self.assertEqual(ok("trial", {"auto_continue": {}})[0], "bad_auto_continue")

    def test_request_stores_options_and_rejects_bad_ones(self):
        self.register("a")
        code, body = self.post({"site_id": "a", "mode": "resume", "requested_by": "op", "reprocess_failed": ["nope"]})
        self.assertEqual((code, body["reason"]), (409, "bad_reprocess_failed"))
        code, body = self.post({"site_id": "a", "mode": "resume", "requested_by": "op",
                                "reprocess_failed": ["empty_item"], "auto_continue": {"max_runs": 3}})
        self.assertEqual(code, 201)
        out = self.r.run_row(self.row(body["run_id"]))
        self.assertEqual((out["reprocess_failed"], out["auto_continue"]), (["empty_item"], {"max_runs": 3, "on": ["time_budget"]}))
        self.assertEqual(out["chain"], {"root": body["run_id"], "seq": 1, "end": None, "reprocess_failed_in": None})
        # Options never bypass the admission rules: the same site is busy now.
        code, body = self.post({"site_id": "a", "mode": "resume", "auto_continue": {}})
        self.assertEqual((code, body["reason"]), (409, "site_busy"))
        self.register("big", approved=False)
        code, body = self.post({"site_id": "big", "mode": "resume", "auto_continue": {}})
        self.assertEqual((code, body["reason"]), (409, "large_needs_approval"))

    def test_plain_request_has_no_options(self):
        self.register("a")
        code, body = self.post({"site_id": "a", "mode": "resume"})
        out = self.r.run_row(self.row(body["run_id"]))
        self.assertEqual((out["reprocess_failed"], out["auto_continue"], out["chain"]), (None, None, None))

    def exec_env(self, run_id):
        out = os.path.join(self.home, "env.json")
        json.dump({"fake_cmd": [sys.executable, "-c", "import json,os,sys; json.dump(os.environ.get('ENUM_EXTRA_ARGS'),"
                                " open(sys.argv[1],'w'))", out]}, open(os.path.join(self.home, "config.json"), "w"))
        with mock.patch.dict(os.environ, {"ENUM_EXTRA_ARGS": "--refresh"}):
            self.assertEqual(self.r._exec(self.c, run_id), 0)
        return json.load(open(out))

    def test_reprocess_failed_reaches_run_pilot_as_extra_args(self):
        self.register("a")
        code, body = self.post({"site_id": "a", "mode": "resume", "reprocess_failed": ["empty_item", "parse_error:"]})
        self.start(body["run_id"])
        self.assertEqual(self.exec_env(body["run_id"]), "--reprocess-failed empty_item --reprocess-failed parse_error:")
        self.c.execute("update runs set status='done' where mode='resume'")
        code, body = self.post({"site_id": "a", "mode": "trial", "limit": 5})
        self.start(body["run_id"])
        self.assertIsNone(self.exec_env(body["run_id"]))  # nothing inherited, nothing added

    def test_tampered_options_are_refused_at_exec(self):
        self.register("a")
        code, body = self.post({"site_id": "a", "mode": "resume"})
        self.c.execute("update runs set opts=? where run_id=?", (json.dumps({"reprocess_failed": ["x --refresh"]}), body["run_id"]))
        self.start(body["run_id"])
        with self.assertRaises(RuntimeError):
            self.r._exec(self.c, body["run_id"])

    # -- chain ----------------------------------------------------------------------
    def chain(self, max_runs=3, site="a", **extra):
        code, body = self.post(dict({"site_id": site, "mode": "resume", "requested_by": "op",
                                     "auto_continue": {"max_runs": max_runs}}, **extra))
        self.assertEqual(code, 201, body)
        return body["run_id"]

    def test_time_budget_continues_in_the_same_folder_until_max_runs(self):
        self.register("a")
        first = self.chain(3, reprocess_failed=["empty_item"])
        self.finish(first)
        second = self.c.execute("select * from runs where status='queued'").fetchone()
        self.assertEqual((second["tag"], second["chain_root"], second["chain_seq"], second["requested_by"]),
                         ("20261001-s1", first, 2, "op"))
        # reprocess_failed only on the first run; later runs resume normally (recorded in the chain info).
        self.assertEqual(json.loads(second["opts"]), {"auto_continue": {"max_runs": 3, "on": ["time_budget"]},
                                                      "chain_reprocessed": {"run_id": first, "prefixes": ["empty_item"]}})
        self.assertEqual(self.r.run_row(second)["chain"]["reprocess_failed_in"], {"run_id": first, "prefixes": ["empty_item"]})
        self.assertIsNone(self.r.run_row(second)["reprocess_failed"])
        self.start(second["run_id"])
        self.assertIsNone(self.exec_env(second["run_id"]))  # no --reprocess-failed on the continuation
        self.finish(second["run_id"])
        third = self.c.execute("select * from runs where status='queued'").fetchone()
        self.assertEqual(third["chain_seq"], 3)
        self.finish(third["run_id"])
        self.assertIsNone(self.c.execute("select 1 from runs where status='queued'").fetchone())
        self.assertEqual(self.row(third["run_id"])["chain_end"], "max_runs")
        end = self.events("chain_end")[-1]
        self.assertEqual((end["root"], end["end"]), (first, "max_runs"))
        self.assertEqual([x["run_id"] for x in end["runs"]], [first, second["run_id"], third["run_id"]])
        self.assertEqual({x["outcome"] for x in end["runs"]}, {"incomplete/time_budget"})
        self.assertEqual([e["seq"] for e in self.events("auto_continue")], [2, 3])
        self.assertEqual(end["reprocess_failed"], {"run_id": first, "prefixes": ["empty_item"]})

    def assert_stops(self, end, rc=0, result=None, cancel=False):
        self.result = result or self.result
        first = self.chain(5)
        if cancel:
            self.c.execute("update runs set cancel_requested=1 where run_id=?", (first,))
        self.finish(first, rc)
        self.assertIsNone(self.c.execute("select 1 from runs where status='queued'").fetchone(), end)
        self.assertEqual(self.row(first)["chain_end"], end)
        self.c.execute("delete from runs")

    def test_chain_stops_on_anything_but_time_budget(self):
        self.register("a")
        self.assert_stops("outcome:complete/None", result={"site_status": "complete", "stop_reason": None})
        self.assert_stops("outcome:incomplete/rate_limited_cooldown",
                          result={"site_status": "incomplete", "stop_reason": "rate_limited_cooldown"})
        # A listing failure whose retry wait did not fit in the time budget ends listing_transient (not
        # time_budget): the chain stops instead of starting another run against a host that is down.
        self.assert_stops("outcome:incomplete/listing_transient:http_503",
                          result={"site_status": "incomplete", "stop_reason": "listing_transient:http_503"})
        self.assert_stops("outcome:incomplete/robots_unavailable:http_503",
                          result={"site_status": "incomplete", "stop_reason": "robots_unavailable:http_503"})
        self.assert_stops("run_failed", rc=2, result={"site_status": "access_blocked", "stop_reason": "access_refused"})
        self.assert_stops("run_failed", rc=1, result={"site_status": "config_error", "stop_reason": "x"})
        self.assert_stops("run_cancelled", result=dict(TIME_BUDGET), cancel=True)

    def test_chain_respects_admission_rules(self):
        self.register("a")
        first = self.chain(5)
        self.c.execute("insert or replace into flags values('paused','1')")
        self.finish(first)
        self.assertEqual(self.row(first)["chain_end"], "refused:runner_paused")
        self.c.execute("insert or replace into flags values('paused','0')")
        first = self.chain(5)
        self.reg["a"]["approved_large"] = False  # approval withdrawn by re-registration
        self.r.save_registry(self.reg)
        self.finish(first)
        self.assertEqual(self.row(first)["chain_end"], "refused:large_needs_approval")
        self.reg["a"].update(approved_large=True, ready=False)
        self.r.save_registry(self.reg)
        self.c.execute("update runs set status='done'")
        self.c.execute("insert into runs(run_id,site_id,mode,status,unit,opts,chain_root,chain_seq,tag) "
                       "values('r0100','a','resume','running','a.test',?,'r0100',1,'20261001-s1')",
                       (json.dumps({"auto_continue": {"max_runs": 5, "on": ["time_budget"]}}),))
        self.r.finish(self.c, self.row("r0100"), 0)
        self.assertEqual(self.row("r0100")["chain_end"], "refused:plan_not_ready")
        self.assertIsNone(self.c.execute("select 1 from runs where status='queued'").fetchone())

    def test_chain_waits_for_a_busy_unit_and_never_runs_two_of_a_site(self):
        self.register("a")
        self.register("b", unit="a.test")
        first = self.chain(5)
        # Another site of the same registered domain is running: queued, and tick does not start it.
        self.c.execute("insert into runs(run_id,site_id,mode,status,unit,tag) values('r0500','b','resume','running','a.test','20261001-s9')")
        with mock.patch.object(self.r, "pid_alive", return_value=True):
            self.finish(first)
            nxt = self.c.execute("select * from runs where status='queued'").fetchone()
            self.assertEqual((nxt["site_id"], nxt["chain_seq"]), ("a", 2))
            with mock.patch.object(self.r.subprocess, "Popen") as popen:
                self.r.tick()
                popen.assert_not_called()
            self.assertEqual(self.row(nxt["run_id"])["status"], "queued")
            self.c.execute("update runs set status='done' where run_id='r0500'")
            with mock.patch.object(self.r.subprocess, "Popen") as popen:
                popen.return_value.pid = 4242
                self.r.tick()
                popen.assert_called_once()
        self.assertEqual((self.row(nxt["run_id"])["status"], self.row(nxt["run_id"])["tag"]), ("running", "20261001-s1"))

    def test_chain_refuses_when_the_site_already_has_a_runner_job(self):
        self.register("a")
        first = self.chain(5)
        self.start(first)
        self.c.execute("insert into runs(run_id,site_id,mode,status,unit) values('r0900','a','trial','queued','a.test')")
        self.r.finish(self.c, self.row(first), 0)
        self.assertEqual(self.row(first)["chain_end"], "refused:site_busy")
        self.assertEqual(self.c.execute("select count(*) from runs where status='queued'").fetchone()[0], 1)

    def test_interrupted_run_is_requeued_not_chained(self):
        self.register("a")
        first = self.chain(5)
        self.r.finish(self.c, self.start(first), None, "interrupted")
        self.assertEqual(self.row(first)["status"], "queued")  # the same job again (existing requeue)
        self.assertEqual(self.c.execute("select count(*) from runs").fetchone()[0], 1)
        self.assertEqual(self.events("auto_continue"), [])

    def test_run_ids_stay_unique_when_two_processes_insert(self):
        self.register("a")
        real = self.r.next_run_id
        calls = []

        def stale(c):
            calls.append(1)
            return "r0001" if len(calls) == 1 else real(c)  # read before the other process's r0001 was committed
        self.r.insert_run(self.c, "a", "trial", 1, "a.test", "op")
        with mock.patch.object(self.r, "next_run_id", stale):
            run_id = self.r.insert_run(self.c, "a", "trial", 1, "a.test", "op")
        self.assertEqual(run_id, "r0002")


if __name__ == "__main__":
    unittest.main()
