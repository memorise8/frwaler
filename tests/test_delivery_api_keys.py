# -*- coding: utf-8 -*-
"""/settings/api-keys against a fake provider transport (no network): a key is saved only after a
successful test request, failures keep the old key, nothing returns or logs the key."""
import json
import logging
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from crawler.enumeration.http import Response, TransportError

OPERATOR_TOKEN = "o" * 32
GOOD = "good_key_1234567890"
OTHER = "other_key_0987654321"
OK_BODY = json.dumps({"meta": {"count": 1}, "results": [{"id": "https://openalex.org/W1"}]}).encode()
NO_RULES = Response(404, {"Content-Type": "text/plain"}, b"")


class Provider:
    """api.openalex.org: 200 for GOOD (and OTHER), 401 for anything else, or a fixed answer."""

    def __init__(self):
        self.calls = []
        self.answer = None

    def request(self, method, url, *, headers=None, data=None):
        self.calls.append((url, dict(headers or {})))
        if url.endswith("/robots.txt"):
            return NO_RULES
        if self.answer is not None:
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer
        if (headers or {}).get("Authorization") in ("Bearer " + GOOD, "Bearer " + OTHER):
            return Response(200, {"Content-Type": "application/json"}, OK_BODY)
        return Response(401, {"Content-Type": "application/json"}, b'{"error":"bad key"}')


class ApiKeysTest(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from delivery.be.api_keys import build_router

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "secrets")
        os.mkdir(self.dir, 0o700)
        env = {"DELIVERY_AUTH_MODE": "token", "DELIVERY_API_TOKEN": OPERATOR_TOKEN, "API_KEYS_DIR": self.dir}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.provider = Provider()
        app = FastAPI()
        app.include_router(build_router(lambda: self.provider, os.path.join(self.tmp.name, "gates"), interval=0))
        self.client = TestClient(app)
        self.h = {"x-delivery-token": OPERATOR_TOKEN}

    def post(self, key, provider="openalex", **kw):
        return self.client.post("/settings/api-keys/" + provider, headers=self.h, content=json.dumps({"key": key}), **kw)

    def key_file(self):
        return os.path.join(self.dir, "openalex.env")

    def rows(self):
        r = self.client.get("/settings/api-keys", headers=self.h)
        self.assertEqual(200, r.status_code)
        return {row["provider"]: row for row in r.json()["providers"]}, r.text

    def test_operator_token_required(self):
        self.assertEqual(401, self.client.get("/settings/api-keys").status_code)
        self.assertEqual(401, self.client.post("/settings/api-keys/openalex", content=b'{"key":"x"}').status_code)

    def test_good_key_is_tested_then_saved_private(self):
        r = self.post(GOOD)
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual("saved", r.json()["result"])
        self.assertNotIn(GOOD, r.text)
        with open(self.key_file()) as f:
            self.assertEqual("OPENALEX_API_KEY=%s\n" % GOOD, f.read())
        self.assertEqual(0o600, stat.S_IMODE(os.stat(self.key_file()).st_mode))
        # one provider request, key only as the Authorization header to api.openalex.org, never in a URL
        api = [(u, h) for u, h in self.provider.calls if "/works" in u]
        self.assertEqual(1, len(api))
        self.assertTrue(api[0][0].startswith("https://api.openalex.org/works?per_page=1"))
        self.assertEqual("Bearer " + GOOD, api[0][1]["Authorization"])
        self.assertTrue(all(GOOD not in u for u, _ in self.provider.calls))
        self.assertIn("LibertreeCollector", api[0][1]["User-Agent"])
        rows, text = self.rows()
        self.assertTrue(rows["openalex"]["configured"])
        self.assertEqual("saved", rows["openalex"]["last_test"]["result"])
        self.assertNotIn(GOOD, text)

    def test_bad_key_is_not_saved_and_old_key_stays(self):
        self.assertEqual(200, self.post(GOOD).status_code)
        r = self.post("wrong_key_000000000")
        self.assertEqual((422, "bad_key"), (r.status_code, r.json()["reason"]))
        self.assertIn("키가 맞지 않습니다", r.json()["detail"])
        with open(self.key_file()) as f:
            self.assertIn(GOOD, f.read())
        rows, _ = self.rows()
        self.assertTrue(rows["openalex"]["configured"])
        self.assertEqual("bad_key", rows["openalex"]["last_test"]["result"])

    def test_rate_limit_is_reported_and_its_cooldown_kept(self):
        self.provider.answer = Response(429, {"Retry-After": "60"}, b"")
        r = self.post(GOOD)
        self.assertEqual((429, "rate_limited"), (r.status_code, r.json()["reason"]))
        self.assertIn("한도 초과", r.json()["detail"])
        # the provider's Retry-After is kept: the next test waits for it without a request
        sent = len(self.provider.calls)
        self.assertEqual("rate_limited", self.post(GOOD).json()["reason"])
        self.assertEqual(sent, len(self.provider.calls))
        self.assertFalse(os.path.exists(self.key_file()))

    def test_connection_failure_and_unexpected_answer(self):
        self.provider.answer = TransportError("ConnectTimeout")
        r = self.post(GOOD)
        self.assertEqual((502, "connect_failed"), (r.status_code, r.json()["reason"]))
        self.assertIn("연결 실패", r.json()["detail"])
        self.provider.answer = Response(200, {"Content-Type": "text/html"}, b"<html>maintenance</html>")
        self.assertEqual("connect_failed", self.post(GOOD).json()["reason"])
        self.assertFalse(os.path.exists(self.key_file()))

    def test_format_and_body_checks_never_echo_the_key(self):
        bad = "has space " + GOOD
        r = self.post(bad)
        self.assertEqual((422, "invalid_format"), (r.status_code, r.json()["reason"]))
        self.assertNotIn(GOOD, r.text)
        self.assertEqual([], self.provider.calls)
        r = self.client.post("/settings/api-keys/openalex", headers=self.h, content=b'{"key": 5}')
        self.assertEqual(400, r.status_code)
        r = self.client.post("/settings/api-keys/openalex", headers=self.h,
                             content=json.dumps({"key": GOOD, "extra": 1}))
        self.assertEqual(400, r.status_code)
        r = self.client.post("/settings/api-keys/openalex", headers=self.h, content=b"x" * 5000)
        self.assertEqual(413, r.status_code)

    def test_attempt_limit_per_provider(self):
        for _ in range(5):
            self.post("wrong_key_000000000")
        r = self.post(GOOD)
        self.assertEqual((429, "too_many_attempts"), (r.status_code, r.json()["reason"]))
        self.assertEqual(5, len([u for u, _ in self.provider.calls if "/works" in u]))
        self.assertFalse(os.path.exists(self.key_file()))
        with open(os.path.join(self.dir, "api-keys-audit.jsonl")) as f:
            audit = [json.loads(l) for l in f]
        self.assertEqual("too_many_attempts", audit[-1]["result"])

    def test_attempt_limit_survives_many_refused_requests(self):
        # review H1: rows written for refused requests must not push the counted tests out of view
        for _ in range(5):
            self.post("wrong_key_000000000")
        for _ in range(210):
            self.post("!!!!!!!!")
        for _ in range(5):
            self.assertEqual("too_many_attempts", self.post(GOOD).json()["reason"])
        self.assertEqual(5, len([u for u, _ in self.provider.calls if "/works" in u]))

    def test_attempt_limit_window_reopens(self):
        import delivery.be.api_keys as K
        for _ in range(5):
            self.post("wrong_key_000000000")
        later = K.time.time() + K.ATTEMPT_WINDOW_S + 1
        with mock.patch.object(K.time, "time", return_value=later):
            self.assertEqual(200, self.post(GOOD).status_code)

    def test_429_without_retry_after_still_waits(self):
        self.provider.answer = Response(429, {}, b"")
        self.assertEqual("rate_limited", self.post(GOOD).json()["reason"])
        self.provider.answer = None
        sent = len(self.provider.calls)
        self.assertEqual("rate_limited", self.post(GOOD).json()["reason"])
        self.assertEqual(sent, len(self.provider.calls))
        self.assertEqual(1, __import__("delivery.be.api_keys", fromlist=["x"]).recent_attempts(self.dir, "openalex"))

    def test_second_test_while_one_runs_is_busy(self):
        import fcntl
        self.post("wrong_key_000000000")  # one test: creates the lock file
        with open(os.path.join(self.dir, ".api-keys.lock")) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            r = self.post(GOOD)
        self.assertEqual((409, "busy"), (r.status_code, r.json()["reason"]))
        self.assertEqual(1, len([u for u, _ in self.provider.calls if "/works" in u]))

    def test_linked_key_file_is_not_followed(self):
        other = os.path.join(self.tmp.name, "elsewhere")
        with open(other, "w") as f:
            f.write("SECRET_TOKEN=abc\nOPENALEX_API_KEY=%s\n" % OTHER)
        os.symlink(other, self.key_file())
        rows, _ = self.rows()
        self.assertFalse(rows["openalex"]["configured"])
        self.assertEqual(200, self.post(GOOD).status_code)
        self.assertFalse(os.path.islink(self.key_file()))
        with open(self.key_file()) as f:
            self.assertEqual("OPENALEX_API_KEY=%s\n" % GOOD, f.read())

    def test_unexpected_error_is_recorded_without_the_key(self):
        import delivery.be.api_keys as K
        with mock.patch.object(K, "save_key", side_effect=RuntimeError("disk " + GOOD)):
            r = self.post(GOOD)
        self.assertEqual((500, "error"), (r.status_code, r.json()["reason"]))
        self.assertNotIn(GOOD, r.text)
        with open(os.path.join(self.dir, "api-keys-audit.jsonl")) as f:
            text = f.read()
        self.assertNotIn(GOOD, text)
        self.assertEqual("RuntimeError", json.loads(text.splitlines()[-1])["reason"])
        self.assertEqual(1, K.recent_attempts(self.dir, "openalex"))

    def test_saved_key_is_reported_saved_even_if_its_record_fails(self):
        import delivery.be.api_keys as K
        with mock.patch.object(K, "record_status", side_effect=OSError("disk full")):
            r = self.post(GOOD)
        self.assertEqual((200, "saved"), (r.status_code, r.json()["result"]))

    def test_linked_lock_and_audit_files_are_replaced(self):
        target = os.path.join(self.tmp.name, "target")
        open(target, "w").close()
        for name in (".api-keys.lock", "api-keys-audit.jsonl"):
            os.symlink(target, os.path.join(self.dir, name))
        self.assertEqual(200, self.post(GOOD).status_code)
        for name in (".api-keys.lock", "api-keys-audit.jsonl"):
            self.assertFalse(os.path.islink(os.path.join(self.dir, name)), name)
        self.assertEqual("", open(target).read())

    def test_long_retry_after_is_capped(self):
        import delivery.be.api_keys as K
        self.provider.answer = Response(429, {"Retry-After": "99999999"}, b"")
        self.post(GOOD)
        until = K.cooldown_until(self.dir, "openalex")
        self.assertLessEqual(until, K.time.time() + K.MAX_COOLDOWN_S + 1)

    def test_placeholder_providers_cannot_save(self):
        rows, _ = self.rows()
        self.assertFalse(rows["data-go-kr"]["testable"])
        self.assertFalse(rows["scienceon"]["testable"])
        self.assertEqual(409, self.post(GOOD, provider="data-go-kr").status_code)
        self.assertEqual(404, self.post(GOOD, provider="nope").status_code)
        self.assertEqual([], self.provider.calls)

    def test_missing_or_open_folder_is_unconfigured(self):
        os.chmod(self.dir, 0o777)
        try:
            self.assertEqual(503, self.post(GOOD).status_code)
            body = self.client.get("/settings/api-keys", headers=self.h).json()
            self.assertFalse(body["available"])
        finally:
            os.chmod(self.dir, 0o700)

    def test_other_lines_kept_and_no_key_in_logs_or_records(self):
        with open(self.key_file(), "w") as f:
            f.write("OTHER_VAR=1\nOPENALEX_API_KEY=%s\n" % GOOD)
        os.chmod(self.key_file(), 0o600)
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("probe").debug("start")
            self.assertEqual(200, self.post(OTHER).status_code)
        with open(self.key_file()) as f:
            self.assertEqual("OTHER_VAR=1\nOPENALEX_API_KEY=%s\n" % OTHER, f.read())
        self.assertNotIn(OTHER, "\n".join(logs.output))
        for name in os.listdir(self.dir):
            if name != "openalex.env":
                with open(os.path.join(self.dir, name)) as f:
                    self.assertNotIn(OTHER, f.read(), name)
                self.assertFalse(name.startswith(".tmp-"), name)


if __name__ == "__main__":
    unittest.main()
