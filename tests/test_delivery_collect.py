# -*- coding: utf-8 -*-
"""/collect routes against a fake runner on 127.0.0.1 and a fake DB connection (no network, no PG)."""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

RUNNER_TOKEN = "runner-test-token"
OPERATOR_TOKEN = "o" * 32


class FakeRunner:
    def __init__(self):
        self.requests = []
        self.post_reply = (201, {"run_id": "r0007"})
        self.runs = [
            {"run_id": "r0003", "site_id": "site-a", "mode": "trial", "limit": 5, "status": "done"},
            {"run_id": "r0002", "site_id": "site-b", "mode": "resume", "limit": None, "status": "failed"},
            {"run_id": "r0001", "site_id": "site-a", "mode": "trial", "limit": 5, "status": "failed"},
        ]
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                fake.requests.append(("GET", self.path, self.headers.get("X-Runner-Token"), None))
                if self.headers.get("X-Runner-Token") != RUNNER_TOKEN:
                    return self.reply(401, {"reason": "bad_token"})
                if self.path == "/runner/health":
                    return self.reply(200, {"paused": False, "max_parallel": 2, "running": 1, "queued": 0})
                if self.path == "/runner/plans":
                    return self.reply(200, [{"site_id": "site-a", "ready": True, "why": None, "strategy": "list",
                                             "approved_large": 1}])
                if self.path == "/runner/garbage":
                    self.wfile.write(b"not-http\r\n\r\n")
                    self.close_connection = True
                    return None
                if self.path.startswith("/runner/runs"):
                    return self.reply(200, fake.runs)
                if self.path == "/runner/redirect":
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:1/elsewhere")
                    self.end_headers()
                    return None
                return self.reply(404, {"reason": "not_found"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                fake.requests.append(("POST", self.path, self.headers.get("X-Runner-Token"), body))
                if self.path.endswith("/cancel"):
                    return self.reply(409, {"reason": "not_active", "detail": "done"})
                return self.reply(*fake.post_reply)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeConn:
    """counts=None: no document_counts table (live GROUP BY); a list: rows of the counts table."""

    def __init__(self, rows, fail=False, counts=None):
        self.rows, self.fail, self.counts = rows, fail, counts

    def execute(self, sql, params=None):
        if self.fail:
            raise RuntimeError("db down")
        self.sql = sql
        return self

    def fetchone(self):
        if "to_regclass" in self.sql:
            return {"relation": None if self.counts is None else "document_counts"}
        return {"present": bool(self.counts)}

    def fetchall(self):
        return self.counts if "FROM document_counts" in self.sql else self.rows

    def close(self):
        pass


def write_csv(path, rows):
    import csv
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, ["site_id", "state", "detail", "updated_at"])
        writer.writeheader()
        writer.writerows(rows)


class CollectRoutesTest(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from delivery.be.cache import TTLCache
        from delivery.be.collect import build_router

        self.runner = FakeRunner()
        self.addCleanup(self.runner.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        write_csv(os.path.join(self.tmp.name, "a-zone.csv"), [
            {"site_id": "site-a", "state": "needs_dev", "detail": "old", "updated_at": "2026-10-10T00:00:00Z"},
            {"site_id": "site-b", "state": "blocked_access", "detail": "403 ext:cloudflare, [사용자 확인 필요]",
             "updated_at": "2026-10-12T00:00:00Z"},
        ])
        write_csv(os.path.join(self.tmp.name, "b-zone.csv"), [
            {"site_id": "site-a", "state": "ready", "detail": "plan ok", "updated_at": "2026-10-15T00:00:00Z"},
            {"site_id": "site-b", "state": "ready", "detail": "stale", "updated_at": "2026-10-01T00:00:00Z"},
            {"site_id": "site-c", "state": "ready", "detail": "plan ok", "updated_at": "2026-10-15T00:00:00Z"},
        ])
        env = {"DELIVERY_AUTH_MODE": "token", "DELIVERY_API_TOKEN": OPERATOR_TOKEN,
               "COLLECT_RUNNER_URL": self.runner.url, "COLLECT_RUNNER_TOKEN": RUNNER_TOKEN,
               "COLLECT_STATUS_DIR": self.tmp.name, "COLLECT_RUNNER_CACHE_SECONDS": "0"}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        self.conn = FakeConn([{"site_id": "site-a", "documents": 12}, {"site_id": "site-c", "documents": 0}])
        app = FastAPI()
        app.include_router(build_router(lambda: self.conn, TTLCache(0)))
        self.client = TestClient(app)
        self.headers = {"x-delivery-token": OPERATOR_TOKEN}

    def test_every_route_requires_operator(self):
        for method, path in (("get", "/collect/runner/health"), ("get", "/collect/plans"), ("get", "/collect/runs"),
                             ("get", "/collect/sites"), ("get", "/collect/uncollected"),
                             ("post", "/collect/runs"), ("post", "/collect/runs/r0001/cancel")):
            kwargs = {"json": {"site_id": "site-a", "mode": "trial"}} if method == "post" else {}
            self.assertEqual(getattr(self.client, method)(path, **kwargs).status_code, 401, path)
            self.assertEqual(getattr(self.client, method)(path, headers={"x-delivery-token": "x" * 32}, **kwargs).status_code, 401)
        self.assertEqual(self.runner.requests, [])

    def test_requested_by_is_the_operator_not_the_client(self):
        response = self.client.post("/collect/runs", headers=self.headers,
                                    json={"site_id": "site-a", "mode": "trial", "limit": 5, "requested_by": "mallory"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), {"run_id": "r0007"})
        method, path, token, body = self.runner.requests[-1]
        self.assertEqual((method, path, token), ("POST", "/runner/runs", RUNNER_TOKEN))
        self.assertEqual(body, {"site_id": "site-a", "mode": "trial", "limit": 5, "requested_by": "customer-operator"})
        cancel = self.client.post("/collect/runs/r0003/cancel", headers=self.headers, json={"requested_by": "mallory"})
        self.assertEqual(self.runner.requests[-1][3], {"requested_by": "customer-operator"})
        self.assertEqual(cancel.status_code, 409)
        self.assertEqual(cancel.json(), {"reason": "not_active", "detail": "done"})

    def test_site_id_mode_limit_and_run_id_validation(self):
        bad = [{"site_id": "../etc", "mode": "trial"}, {"site_id": "a" * 121, "mode": "trial"},
               {"site_id": "site a", "mode": "trial"}, {"site_id": 5, "mode": "trial"},
               {"site_id": "site-a", "mode": "full"}, {"site_id": "site-a"},
               {"site_id": "site-a", "mode": "trial", "limit": 0}, {"site_id": "site-a", "mode": "trial", "limit": True},
               {"site_id": "site-a", "mode": "trial", "limit": "5"}]
        for body in bad:
            response = self.client.post("/collect/runs", headers=self.headers, json=body)
            self.assertEqual(response.status_code, 422, body)
            self.assertIn(response.json()["reason"], ("bad_site_id", "bad_mode", "bad_limit"))
        self.assertEqual(self.client.post("/collect/runs/x1/cancel", headers=self.headers).json()["reason"], "bad_run_id")
        self.assertEqual(self.client.get("/collect/runs", params={"site_id": "a&b=c"}, headers=self.headers).status_code, 422)
        self.assertEqual(self.runner.requests, [])
        self.assertEqual(self.client.get("/collect/runs", params={"site_id": "site-a"}, headers=self.headers).status_code, 200)
        self.assertEqual(self.runner.requests[-1][1], "/runner/runs?site_id=site-a")

    def test_409_reason_passes_through(self):
        self.runner.post_reply = (409, {"reason": "unit_busy", "detail": "같은 등록 도메인(x) 작업이 대기·실행 중"})
        response = self.client.post("/collect/runs", headers=self.headers, json={"site_id": "site-a", "mode": "resume"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"reason": "unit_busy", "detail": "같은 등록 도메인(x) 작업이 대기·실행 중"})
        self.assertNotIn("limit", self.runner.requests[-1][3])

    def test_resume_options_pass_through_to_the_runner(self):
        options = {"reprocess_failed": ["empty_item"], "auto_continue": {"max_runs": 5, "on": ["time_budget"]}}
        response = self.client.post("/collect/runs", headers=self.headers, json=dict({"site_id": "site-a", "mode": "resume"}, **options))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.runner.requests[-1][3], dict({"site_id": "site-a", "mode": "resume",
                                                            "requested_by": "customer-operator"}, **options))
        sent = len(self.runner.requests)
        for bad in ({"reprocess_failed": "empty_item"}, {"reprocess_failed": [1]}, {"auto_continue": True}):
            response = self.client.post("/collect/runs", headers=self.headers, json=dict({"site_id": "site-a", "mode": "resume"}, **bad))
            self.assertEqual(response.status_code, 422, bad)
        self.assertEqual(len(self.runner.requests), sent)  # malformed options never reach the runner
        self.runner.post_reply = (409, {"reason": "bad_reprocess_failed", "detail": "x"})
        response = self.client.post("/collect/runs", headers=self.headers,
                                    json={"site_id": "site-a", "mode": "resume", "reprocess_failed": ["identity_collision"]})
        self.assertEqual((response.status_code, response.json()["reason"]), (409, "bad_reprocess_failed"))

    def test_proxies_health_plans_runs(self):
        health = self.client.get("/collect/runner/health", headers=self.headers).json()
        self.assertEqual(health, {"runner_ok": True, "reason": None, "detail": None,
                                  "paused": False, "max_parallel": 2, "running": 1, "queued": 0})
        self.assertEqual(self.client.get("/collect/plans", headers=self.headers).json()[0]["site_id"], "site-a")
        self.assertEqual(len(self.client.get("/collect/runs", headers=self.headers).json()), 3)

    def test_bad_runner_token_and_redirects_are_not_followed(self):
        from delivery.be.collect import RunnerError, runner_call
        with mock.patch.dict(os.environ, {"COLLECT_RUNNER_TOKEN": "wrong"}):
            response = self.client.get("/collect/plans", headers=self.headers)
            self.assertEqual((response.status_code, response.json()["reason"]), (502, "runner_auth_failed"))
        with self.assertRaises(RunnerError) as caught:
            runner_call("GET", "/runner/redirect")
        self.assertEqual(caught.exception.reason, "runner_bad_response")
        self.assertEqual([r[1] for r in self.runner.requests].count("/elsewhere"), 0)

    def test_malformed_runner_response_is_runner_unavailable(self):
        from delivery.be.collect import RunnerError, runner_call
        with self.assertRaises(RunnerError) as caught:
            runner_call("GET", "/runner/garbage")
        self.assertEqual(caught.exception.reason, "runner_unavailable")

    def test_runner_snapshot_is_cached_briefly(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from delivery.be.cache import TTLCache
        from delivery.be.collect import build_router
        with mock.patch.dict(os.environ, {"COLLECT_RUNNER_CACHE_SECONDS": "60"}):
            app = FastAPI()
            app.include_router(build_router(lambda: self.conn, TTLCache(0)))
        client = TestClient(app)
        client.get("/collect/sites", headers=self.headers)
        client.get("/collect/uncollected", headers=self.headers)
        self.assertEqual([r[1] for r in self.runner.requests], ["/runner/runs", "/runner/plans"])

    def test_token_file_and_unconfigured(self):
        token_file = os.path.join(self.tmp.name, "token.txt")
        with open(token_file, "w") as handle:
            handle.write(RUNNER_TOKEN + "\n")
        with mock.patch.dict(os.environ, {"COLLECT_RUNNER_TOKEN": "", "COLLECT_RUNNER_TOKEN_FILE": token_file}):
            self.assertEqual(self.client.get("/collect/plans", headers=self.headers).status_code, 200)
        with mock.patch.dict(os.environ, {"COLLECT_RUNNER_TOKEN": "", "COLLECT_RUNNER_TOKEN_FILE": ""}):
            response = self.client.get("/collect/plans", headers=self.headers)
            self.assertEqual((response.status_code, response.json()["reason"]), (503, "runner_unconfigured"))
            self.assertFalse(self.client.get("/collect/runner/health", headers=self.headers).json()["runner_ok"])

    def test_sites_merge_latest_judgment_counts_and_latest_run(self):
        data = self.client.get("/collect/sites", headers=self.headers).json()
        self.assertTrue(data["runner_ok"])
        self.assertTrue(data["documents_ok"])
        self.assertEqual(data["status_source"], {"ok": True, "reason": None, "files": 2})
        sites = {s["site_id"]: s for s in data["sites"]}
        self.assertEqual(sorted(sites), ["site-a", "site-b", "site-c"])
        a, b = sites["site-a"], sites["site-b"]
        self.assertEqual((a["state"], a["class"], a["status_file"], a["documents"]), ("ready", "run", "b-zone.csv", 12))
        self.assertEqual(a["latest_run"]["run_id"], "r0003")
        self.assertTrue(a["plan"]["ready"])
        self.assertIs(a["plan"]["approved_large"], True)
        self.assertEqual((b["state"], b["class"], b["class_label"], b["status_file"]), ("blocked_access", "nego", "협의 필요", "a-zone.csv"))
        self.assertEqual(b["reason_codes"], ["ext:cloudflare", "user_decision"])
        self.assertEqual(b["documents"], 0)
        self.assertEqual(b["latest_run"]["run_id"], "r0002")
        self.assertIsNone(sites["site-c"]["latest_run"])
        self.assertIn("GROUP BY site_id", self.conn.sql)

    def test_sites_degrade_when_runner_down_or_status_missing(self):
        with mock.patch.dict(os.environ, {"COLLECT_RUNNER_URL": "http://127.0.0.1:1"}):
            data = self.client.get("/collect/sites", headers=self.headers).json()
        self.assertFalse(data["runner_ok"])
        self.assertEqual(data["runner_reason"], "runner_unavailable")
        self.assertEqual(len(data["sites"]), 3)
        self.assertTrue(all(s["latest_run"] is None for s in data["sites"]))
        with mock.patch.dict(os.environ, {"COLLECT_STATUS_DIR": os.path.join(self.tmp.name, "nope")}):
            data = self.client.get("/collect/sites", headers=self.headers).json()
        self.assertEqual(data["status_source"]["reason"], "missing")
        self.assertEqual(sorted(s["site_id"] for s in data["sites"]), ["site-a", "site-b"])  # from runner only
        self.conn.fail = True
        data = self.client.get("/collect/sites", headers=self.headers).json()
        self.assertFalse(data["documents_ok"])
        self.assertIsNone(data["sites"][0]["documents"])

    def write_progress(self, rows):
        import csv
        os.makedirs(os.path.join(self.tmp.name, "runs"), exist_ok=True)
        with open(os.path.join(self.tmp.name, "runs", "O1-full.csv"), "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, ["site_id", "zone", "expected", "status", "run_id", "run_folder", "source",
                                             "started", "finished", "result", "cap_hit", "group"])
            writer.writeheader()
            writer.writerows(rows)

    def test_db_only_sites_and_counts_table(self):
        self.conn.counts = [{"site_id": "site-a", "documents": 12}, {"site_id": "scienceon-api", "documents": 3400000},
                            {"site_id": "db-site", "documents": 7}]
        data = self.client.get("/collect/sites", headers=self.headers).json()
        self.assertIn("FROM document_counts", self.conn.sql)
        sites = {s["site_id"]: s for s in data["sites"]}
        self.assertEqual(sorted(sites), ["db-site", "scienceon-api", "site-a", "site-b", "site-c"])
        sci, other = sites["scienceon-api"], sites["db-site"]
        self.assertEqual((sci["source"], sci["class"], sci["class_label"], sci["documents"]), ("db_only", "", "API 수집", 3400000))
        self.assertEqual((other["class_label"], other["documents"]), ("판정 없음", 7))
        self.assertEqual(sites["site-a"]["source"], "status")
        self.assertEqual(sites["site-c"]["documents"], 0)  # not in the counts table
        unc = {r["site_id"] for r in self.client.get("/collect/uncollected", headers=self.headers).json()["sites"]}
        self.assertNotIn("scienceon-api", unc)
        self.assertNotIn("db-site", unc)

    def test_direct_progress_from_o1_status(self):
        data = self.client.get("/collect/sites", headers=self.headers).json()
        self.assertEqual(data["progress_source"], {"ok": False, "reason": "missing", "updated_at": None})
        self.assertIsNone(data["sites"][0]["direct"])
        self.write_progress([
            {"site_id": "site-a", "status": "direct_running", "run_id": "direct", "run_folder": "20261019-s155",
             "started": "2026-10-20T22:04:49Z", "finished": "2026-10-20T22:04:48Z",
             "result": "시간 예산으로 끊김, 같은 방식으로 이어받음 — incomplete/time_budget, 문서 354455, 날짜 2006-01-27~2026-10-20"},
            {"site_id": "site-b", "status": "done", "run_id": "r0575", "result": "complete/x, 문서 1,234, 날짜 None~None"},
            {"site_id": "site-z", "status": "failed", "result": "no count here"},
        ])
        data = self.client.get("/collect/sites", headers=self.headers).json()
        self.assertTrue(data["progress_source"]["ok"])
        self.assertRegex(data["progress_source"]["updated_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        sites = {s["site_id"]: s for s in data["sites"]}
        self.assertNotIn("site-z", sites)  # the progress file never adds sites by itself
        a = sites["site-a"]["direct"]
        self.assertEqual((a["status"], a["running"], a["saved"], a["run_folder"]), ("direct_running", True, 354455, "20261019-s155"))
        self.assertEqual(a["updated_at"], data["progress_source"]["updated_at"])
        self.assertEqual(a["finished_at"], "2026-10-20T22:04:48Z")
        self.assertEqual((sites["site-b"]["direct"]["running"], sites["site-b"]["direct"]["saved"]), (False, 1234))
        self.assertEqual(data["status_source"]["files"], 2)  # runs/O1-full.csv is not a judgment file
        with open(os.path.join(self.tmp.name, "runs", "O1-full.csv"), "wb") as handle:
            handle.write(b"\xff\xfe\x00bad")
        data = self.client.get("/collect/sites", headers=self.headers).json()
        self.assertEqual(data["progress_source"]["reason"], "unreadable")
        self.assertEqual(len(data["sites"]), 3)

    def test_uncollected_filter_and_notes(self):
        data = self.client.get("/collect/uncollected", headers=self.headers).json()
        rows = {r["site_id"]: r for r in data["sites"]}
        self.assertNotIn("site-a", rows)  # class run with documents
        self.assertEqual(rows["site-b"]["uncollected_reasons"], ["class_nego", "no_documents"])
        self.assertEqual(rows["site-c"]["uncollected_reasons"], ["no_documents"])
        self.assertEqual(rows["meri-belspo-be-site"]["class"], "")
        self.assertTrue(rows["meri-belspo-be-site"]["note"])
        self.assertEqual(data["count"], len(data["sites"]))
        self.assertEqual(data["sites"][0]["site_id"], "site-b")  # nego sorts first
        self.assertEqual(data["by_class"]["nego"], 1)
        self.assertTrue(data["notes_ok"])
        with mock.patch("delivery.be.collect.NOTES_PATH", os.path.join(self.tmp.name, "missing.json")):
            data = self.client.get("/collect/uncollected", headers=self.headers).json()
        self.assertFalse(data["notes_ok"])
        self.assertNotIn("meri-belspo-be-site", {r["site_id"] for r in data["sites"]})


ROOT = os.path.join(os.path.dirname(__file__), "..")


class CollectRunnerNonRootTest(unittest.TestCase):
    """run-pilot.sh refuses uid 0, so the containerised runner must work without root."""

    def test_compose_and_dockerfile_run_the_runner_as_non_root(self):
        with open(os.path.join(ROOT, "delivery", "docker-compose.yml"), encoding="utf-8") as handle:
            compose = handle.read()
        runner = compose.split("\n  runner:\n", 1)[1].split("\nvolumes:\n", 1)[0]
        self.assertIn('user: "${COLLECT_RUNNER_UID:-1000}:${COLLECT_RUNNER_GID:-1000}"', runner)
        self.assertIn('group_add: ["${DOCKER_GID:-999}"]', runner)
        self.assertIn('profiles: ["runner"]', runner)
        self.assertNotIn("ports:", runner)
        with open(os.path.join(ROOT, "delivery", "Dockerfile.runner"), encoding="utf-8") as handle:
            dockerfile = handle.read()
        self.assertIn("HOME=/tmp", dockerfile)
        self.assertRegex(dockerfile, r"(?m)^USER 1000:1000$")

    @unittest.skipIf(os.geteuid() == 0, "needs a non-root uid")
    def test_runner_serves_from_a_fresh_home_as_non_root(self):
        import socket
        import subprocess
        import time
        import urllib.request
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with tempfile.TemporaryDirectory() as home:
            # Same shape as the container: config seeded into RUNNER_HOME, docker unusable.
            with open(os.path.join(ROOT, "delivery", "collect-runner.config.json"), encoding="utf-8") as handle:
                config = json.load(handle)
            config.update({"port": port, "docker": "/bin/false"})
            with open(os.path.join(home, "config.json"), "w", encoding="utf-8") as handle:
                json.dump(config, handle)
            proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "deploy", "collect-runner", "runner.py"), "serve"],
                                    env=dict(os.environ, RUNNER_HOME=home, RUNNER_BIND="127.0.0.1", HOME="/tmp"),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                token_path = os.path.join(home, ".token")
                for _ in range(50):
                    if os.path.exists(token_path):
                        try:
                            request = urllib.request.Request("http://127.0.0.1:%d/runner/health" % port,
                                                             headers={"X-Runner-Token": open(token_path).read().strip()})
                            health = json.load(urllib.request.urlopen(request, timeout=2))
                            break
                        except OSError:
                            pass
                    time.sleep(0.1)
                else:
                    self.fail("runner did not come up")
                self.assertEqual(health["running"], 0)
                self.assertEqual(os.stat(token_path).st_mode & 0o777, 0o600)
                self.assertTrue(os.path.isfile(os.path.join(home, "runner.sqlite")))
            finally:
                proc.terminate()
                proc.wait(timeout=10)

    @unittest.skipIf(os.geteuid() == 0, "needs a non-root uid")
    def test_be_reaches_the_runner_over_its_unix_socket(self):
        import socket
        import subprocess
        import time
        from unittest import mock
        from delivery.be import collect
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with tempfile.TemporaryDirectory() as home:
            with open(os.path.join(ROOT, "delivery", "collect-runner.config.json"), encoding="utf-8") as handle:
                config = json.load(handle)
            config.update({"port": port, "docker": "/bin/false"})
            with open(os.path.join(home, "config.json"), "w", encoding="utf-8") as handle:
                json.dump(config, handle)
            sock_path = os.path.join(home, "sock", "runner.sock")
            proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "deploy", "collect-runner", "runner.py"), "serve"],
                                    env=dict(os.environ, RUNNER_HOME=home, RUNNER_BIND="127.0.0.1",
                                             RUNNER_SOCKET=sock_path, HOME="/tmp"),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                token_path = os.path.join(home, ".token")
                for _ in range(50):
                    if os.path.exists(sock_path) and os.path.exists(token_path):
                        break
                    time.sleep(0.1)
                else:
                    self.fail("runner socket did not appear")
                self.assertEqual(os.stat(sock_path).st_mode & 0o777, 0o660)
                token = open(token_path).read().strip()
                env = {"COLLECT_RUNNER_SOCKET": sock_path, "COLLECT_RUNNER_TOKEN": token,
                       "COLLECT_RUNNER_URL": "http://unreachable.invalid:1"}
                with mock.patch.dict(os.environ, env):
                    status, payload = collect.runner_call("GET", "/runner/health")
                self.assertEqual(status, 200)
                self.assertEqual(payload["running"], 0)
                with mock.patch.dict(os.environ, dict(env, COLLECT_RUNNER_TOKEN="wrong")):
                    with self.assertRaises(collect.RunnerError) as caught:
                        collect.runner_call("GET", "/runner/health")
                self.assertEqual(caught.exception.reason, "runner_auth_failed")
                with mock.patch.dict(os.environ, dict(env, COLLECT_RUNNER_SOCKET=sock_path + ".missing")):
                    with self.assertRaises(collect.RunnerError) as caught:
                        collect.runner_call("GET", "/runner/health")
                self.assertEqual(caught.exception.reason, "runner_unavailable")
            finally:
                proc.terminate()
                proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
