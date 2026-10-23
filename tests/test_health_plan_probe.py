"""delivery.health.plan_probe: listing page 1 only, governed Fetcher, fake transport (no network)."""
import contextlib
import fcntl
import io
import json
import shutil
import tempfile
import threading
import time as real_time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from crawler.enumeration import http
from crawler.enumeration.http import Response
from delivery.health import plan_probe, request_gate, runner
from delivery.health.results import read_result

ROBOTS = "https://board.test/robots.txt"
LIST_HTML = ("<html><head><title>list</title></head><body><table><tbody>"
             "<tr><td><a href='/view?id=1'>First</a></td></tr>"
             "<tr><td><a href='/view?id=2'>Second</a></td></tr>"
             "</tbody></table></body></html>")
CHALLENGE = "<html><head><title>Just a moment...</title></head><body>checking</body></html>"


def html(body, status=200, url=""):
    return Response(status, {"content-type": "text/html; charset=utf-8"}, body, url)


def plan_json(site_id, host="https://board.test", **listing):
    return {"version": 1, "sites": {site_id: {
        "strategy": "last_page_backward", "base_url": host, "adapter": {"type": "list_only"},
        "listing": {"url": f"{host}/list", "page_param": "page", "start_page": 1,
                    "parser": {"type": "html", "item_container": "tbody tr", "item_link": "a[href]",
                               "id_regex": r"id=(\d+)"}, **listing}}}}


class FakeTime:
    """One clock for the gate, the Fetcher and the request log; sleeping advances it."""

    def __init__(self):
        self.now = 1_800_000_000.0
        self._lock = threading.Lock()

    def time(self):
        with self._lock:
            return self.now

    monotonic = time

    def sleep(self, seconds):
        with self._lock:
            self.now += max(0.0, seconds)


class FakeTransport:
    """handler(url) -> Response. Records requests and how many were in flight at once."""

    def __init__(self, handler, clock, response_s=0.2):
        self.handler, self.clock, self.response_s = handler, clock, response_s
        self.requests, self.spans = [], []
        self.active = self.max_active = 0
        self._lock = threading.Lock()

    def request(self, method, url, *, headers=None, **kwargs):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.requests.append((method, url, dict(headers or {})))
        start = self.clock.time()
        try:
            real_time.sleep(0.02)  # room for another thread to overlap if the gate did not serialize
            self.clock.sleep(self.response_s)
            return self.handler(url)
        finally:
            self.spans.append((start, self.clock.time()))
            with self._lock:
                self.active -= 1


class PlanProbeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.plans, self.report, self.gates = root / "plans", root / "report", root / "gates"
        self.plans.mkdir()
        self.clock = FakeTime()
        self.patches = contextlib.ExitStack()
        for module in (request_gate, http, plan_probe):
            self.patches.enter_context(patch.object(module, "time", self.clock))
        self.patches.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def tearDown(self):
        self.patches.close()
        self.tmp.cleanup()

    def add_plan(self, site_id, **listing):
        (self.plans / f"{site_id}.json").write_text(json.dumps(plan_json(site_id, **listing)))

    def scan(self, handler, *, workers=1, **kwargs):
        transport = FakeTransport(handler, self.clock)
        scan = plan_probe.run_scan(self.plans, self.report, self.gates, workers=workers,
                                   transport_factory=lambda plan: transport, registered=(),
                                   source_hashes=kwargs.pop("source_hashes", {}), **kwargs)
        return transport, scan

    def result(self, site_id):
        return json.loads((self.report / f"{site_id}.json").read_text())

    def log(self):
        return [json.loads(line) for line in (self.report / "requests.jsonl").read_text().splitlines()]

    def router(self, page, robots=None):
        def handler(url):
            if url == ROBOTS:
                return Response(200, {"content-type": "text/plain"}, robots) if robots else Response(404)
            return page(url)
        return handler

    def test_healthy_page_counts_items_and_reader_shows_it(self):
        self.add_plan("a-site")
        transport, scan = self.scan(self.router(lambda url: html(LIST_HTML, url=url)),
                                    source_hashes={"a-site": "h1"})
        result = self.result("a-site")
        self.assertEqual((result["state"], result["sample_count"], result["invalid_count"]), ("healthy", 2, 0))
        self.assertEqual(result["scope"], "listing_page1_only")
        self.assertEqual([u for _, u, _ in transport.requests], [ROBOTS, "https://board.test/list?page=1"])
        ua = transport.requests[-1][2]["User-Agent"]
        self.assertTrue(ua.startswith("LibertreeCollector/") and "@" not in ua and "Mozilla" not in ua)
        now = datetime.fromisoformat(result["checked_at"])
        shown = read_result(self.report, {"site_id": "a-site", "source_hash": "h1"}, now=now)
        self.assertEqual((shown["state"], shown["sample_count"]), ("healthy", 2))
        self.assertEqual(scan["totals"], {"healthy": 1})
        self.assertEqual(scan["requests"], 2)

    def test_robots_disallow_is_blocked_without_page_request(self):
        self.add_plan("a-site")
        transport, _ = self.scan(self.router(lambda url: html(LIST_HTML), robots="User-agent: *\nDisallow: /list\n"))
        result = self.result("a-site")
        self.assertEqual((result["state"], result["reason"]), ("blocked", "robots_disallowed"))
        self.assertEqual([u for _, u, _ in transport.requests], [ROBOTS])
        self.assertEqual([(r["kind"], r["outcome"]) for r in self.log()], [("robots", "robots")])

    def test_429_is_blocked_after_one_request_and_host_cools_down(self):
        self.add_plan("a-site")
        self.add_plan("b-site")
        transport, _ = self.scan(self.router(lambda url: Response(429, {"retry-after": "5"})))
        first, second = self.result("a-site"), self.result("b-site")
        self.assertEqual(first["state"], "blocked")
        self.assertEqual(second["state"], "blocked")
        self.assertEqual(second["requests"], 0)  # same host: nothing sent after the 429
        self.assertEqual([u for _, u, _ in transport.requests], [ROBOTS, "https://board.test/list?page=1"])
        self.assertEqual(self.log()[-1]["outcome"], "429")

    def test_403_and_challenge_are_blocked_without_retry(self):
        for name, page, reason in (("forbidden", lambda url: html("no", 403), "access_refused"),
                                   ("challenge", lambda url: html(CHALLENGE, url=url), "challenge")):
            with self.subTest(name):
                for f in self.plans.glob("*.json"):
                    f.unlink()
                shutil.rmtree(self.gates, ignore_errors=True)
                self.add_plan(name)
                transport, _ = self.scan(self.router(page))
                result = self.result(name)
                self.assertEqual(result["state"], "blocked")
                self.assertTrue(result["reason"].startswith(reason), result["reason"])
                self.assertEqual(len(transport.requests), 2)  # robots + one listing request
                self.assertEqual(self.log()[-1]["outcome"], "blocked" if name == "forbidden" else "challenge")

    def test_5xx_is_network_error_without_retry(self):
        self.add_plan("a-site")
        transport, _ = self.scan(self.router(lambda url: Response(503)))
        self.assertEqual(self.result("a-site")["state"], "network_error")
        self.assertEqual(len(transport.requests), 2)

    def test_same_host_sites_are_serialized_with_gap(self):
        self.add_plan("a-site")
        self.add_plan("b-site")
        transport, scan = self.scan(self.router(lambda url: html(LIST_HTML, url=url)), workers=2)
        self.assertEqual(len(transport.requests), 4)
        self.assertEqual(transport.max_active, 1)
        spans = sorted(transport.spans)
        gaps = [b[0] - a[1] for a, b in zip(spans, spans[1:])]
        self.assertTrue(all(gap >= 1.5 - 1e-6 for gap in gaps), gaps)
        self.assertGreaterEqual(scan["per_host_min_gap_s"]["board.test"], 1.5)
        lines = sorted(self.log(), key=lambda r: r["at"])
        ends = [datetime.fromisoformat(r["at"]).timestamp() + r["elapsed_s"] for r in lines]
        starts = [datetime.fromisoformat(r["at"]).timestamp() for r in lines]
        self.assertTrue(all(s - e >= 1.5 - 1e-3 for e, s in zip(ends, starts[1:])))

    def test_excluded_site_is_not_requested_and_has_no_file(self):
        self.add_plan("a-site")
        self.add_plan("skip-me")
        transport, scan = self.scan(self.router(lambda url: html(LIST_HTML, url=url)),
                                    excludes={"skip-me": "operator_request"})
        self.assertFalse((self.report / "skip-me.json").exists())
        self.assertTrue((self.report / "a-site.json").exists())
        self.assertEqual(scan["skipped"], {"operator_request": 1})
        self.assertEqual({r["site_id"] for r in self.log()}, {"a-site"})
        self.assertEqual(len(transport.requests), 2)

    def test_log_lines_have_no_url_or_query(self):
        self.add_plan("a-site", params={"menuSn": "23", "token": "secret"})
        transport, _ = self.scan(self.router(lambda url: html(LIST_HTML, url=url)))
        self.assertIn("?", transport.requests[-1][1])
        text = (self.report / "requests.jsonl").read_text()
        for word in ("?", "menuSn", "secret", "http", "/list"):
            self.assertNotIn(word, text)
        for line in self.log():
            self.assertEqual(set(line), {"at", "site_id", "host", "method", "status", "outcome", "elapsed_s", "kind"})
            self.assertTrue(line["at"].endswith("+00:00"))

    def test_dry_run_makes_no_request(self):
        self.add_plan("a-site", params={"menuSn": "23"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(http.RequestsTransport, "request",
                                                           side_effect=AssertionError("network")):
            plan_probe.dry_run(self.plans, ["a-site"])
        line = json.loads(out.getvalue())
        self.assertEqual(line["request"], {"method": "GET", "host": "board.test", "path": "/list",
                                           "query_keys": ["menuSn", "page"]})
        self.assertFalse(self.report.exists())

    def test_cli_refuses_a_second_scan(self):
        self.report.mkdir()
        with (self.report / ".scan.lock").open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with patch.object(plan_probe, "run_scan", side_effect=AssertionError("scan")), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stop:
                plan_probe.main(["--plans-dir", str(self.plans), "--report-dir", str(self.report),
                                 "--gate-dir", str(self.gates)])
        self.assertEqual(stop.exception.code, 2)

    def test_cli_rejects_short_recheck_interval(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            plan_probe.main(["--plans-dir", str(self.plans), "--report-dir", str(self.report),
                             "--gate-dir", str(self.gates), "--interval-hours", "24"])


class LegacyRunnerRefusesTest(unittest.TestCase):
    def test_live_selections_exit_before_any_work(self):
        for argv in (["--all"], ["--all-registered"], ["--site", "x"], ["--all", "--interval", "86400"]):
            with self.subTest(argv), patch.object(runner, "inventory", side_effect=AssertionError("inventory")):
                err = io.StringIO()
                with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stop:
                    runner.main(argv)
                self.assertEqual(stop.exception.code, 2)
                self.assertIn("delivery.health.plan_probe", err.getvalue())

    def test_audit_still_works(self):
        data = {"sites": [{"site_id": "s", "code_status": "ready"}], "summary": {"selected": 1}}
        with tempfile.TemporaryDirectory() as folder, patch.object(runner, "inventory", return_value=data), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(["--audit", "--report-dir", folder]), 0)
            self.assertTrue((Path(folder) / "inventory.json").exists())

    def test_old_probe_clis_refuse(self):
        from delivery.health import listing_probe, probe, source_observer
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(probe.main(), 2)
            for module in (listing_probe, source_observer):
                with self.subTest(module.__name__), patch("sys.argv", ["x", "--inventory", "i", "--out", "o"]), \
                        self.assertRaises(SystemExit) as stop:
                    module.main()
                self.assertEqual(stop.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
