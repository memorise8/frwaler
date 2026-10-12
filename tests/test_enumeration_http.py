"""Request gate, budgets, retries and URL assembly (fake transports, loopback only)."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from crawler.enumeration.http import (AccessBlocked, BudgetExhausted, Fetcher, PermanentFailure,  # noqa: E402
                                      Response, TransientFailure, TransportError)
from crawler.enumeration.urls import build_url, decode_preencoded  # noqa: E402
from tests.enumeration_fakes import FakeTransport  # noqa: E402


class UrlTest(unittest.TestCase):
    def test_preencoded_base_query_is_kept_single_encoded(self):
        url = build_url("https://x.test/list?f%5B0%5D=facet%3A1&q=a+b", {"page": 2})
        self.assertEqual(url, "https://x.test/list?f%5B0%5D=facet%3A1&q=a+b&page=2")

    def test_preencoded_param_keys_are_decoded_once(self):
        url = build_url("https://x.test/list", {"type%5B170%5D": "170", "f[1]": "a:b"})
        self.assertEqual(url, "https://x.test/list?type%5B170%5D=170&f%5B1%5D=a%3Ab")
        self.assertNotIn("%25", url)

    def test_literal_percent_value_survives(self):
        self.assertEqual(decode_preencoded({"q": "100%"}), {"q": "100%"})

    def test_params_override_and_drop(self):
        self.assertEqual(build_url("https://x.test/l?page=3&a=1", {"page": 4}), "https://x.test/l?page=4&a=1")
        self.assertEqual(build_url("https://x.test/l?page=3&a=1", {}, drop={"page"}), "https://x.test/l?a=1")


def ok(body=b"<html><body><table></table></body></html>", **headers):
    return Response(200, {"Content-Type": "text/html", **headers}, body)


class FetcherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sleeps = []

    def tearDown(self):
        self.tmp.cleanup()

    def fetcher(self, handler, **kw):
        kw.setdefault("interval", 0)
        kw.setdefault("respect_robots", False)
        kw.setdefault("sleep", self.sleeps.append)
        transport = FakeTransport(handler)
        return Fetcher("s", transport, Path(self.tmp.name) / "gates", **kw), transport

    def test_404_is_permanent_and_not_retried(self):
        f, t = self.fetcher(lambda u: Response(404, {}, b""))
        with self.assertRaises(PermanentFailure) as ctx:
            f.get("https://a.test/x")
        self.assertEqual(ctx.exception.reason, "http_404")
        self.assertEqual(len(t.urls), 1)

    def test_5xx_and_transport_errors_retry_then_transient(self):
        f, t = self.fetcher(lambda u: Response(502, {}, b""), retries=3)
        with self.assertRaises(TransientFailure):
            f.get("https://a.test/x")
        self.assertEqual(len(t.urls), 3)
        self.assertEqual(len(self.sleeps), 2)
        answers = [TransportError("ConnectTimeout"), ok()]
        f, t = self.fetcher(lambda u: answers.pop(0))
        self.assertEqual(f.get("https://a.test/y").status, 200)

    def test_429_honours_retry_after_and_bounds_it(self):
        answers = [Response(429, {"Retry-After": "7"}, b""), ok()]
        f, _ = self.fetcher(lambda u: answers.pop(0))
        self.assertEqual(f.get("https://a.test/x").status, 200)
        self.assertGreaterEqual(self.sleeps[0], 6)
        f, t = self.fetcher(lambda u: Response(429, {"Retry-After": "86400"}, b""), max_retry_wait_s=120)
        with self.assertRaises(TransientFailure) as ctx:
            f.get("https://a.test/x")
        self.assertEqual((ctx.exception.reason, len(t.urls)), ("rate_limited", 1))

    def test_refusal_blocks_every_later_request(self):
        f, t = self.fetcher(lambda u: Response(403, {}, b"denied"))
        with self.assertRaises(AccessBlocked):
            f.get("https://a.test/x")
        with self.assertRaises(AccessBlocked):
            f.get("https://a.test/other")
        self.assertEqual(len(t.urls), 1)

    def test_authentication_and_login_redirect(self):
        f, _ = self.fetcher(lambda u: Response(401, {}, b""))
        with self.assertRaises(AccessBlocked) as ctx:
            f.get("https://a.test/x")
        self.assertEqual(ctx.exception.reason, "authentication_required")
        f, _ = self.fetcher(lambda u: Response(200, {"Content-Type": "text/html"}, b"<html></html>",
                                               url="https://a.test/user/login?dest=x"))
        with self.assertRaises(AccessBlocked) as ctx:
            f.get("https://a.test/x")
        self.assertEqual(ctx.exception.reason, "login_redirect")

    def test_robots_rules(self):
        def handler(url):
            if url.endswith("/robots.txt"):
                return Response(200, {}, b"User-agent: *\nDisallow: /private/\n")
            return ok()
        f, t = self.fetcher(handler, respect_robots=True)
        self.assertEqual(f.get("https://a.test/public").status, 200)
        with self.assertRaises(AccessBlocked) as ctx:
            f.get("https://a.test/private/1")
        self.assertEqual(ctx.exception.reason, "robots_disallowed")
        self.assertEqual(sum(u.endswith("robots.txt") for u in t.urls), 1)
        f, _ = self.fetcher(lambda u: Response(503, {}, b""), respect_robots=True, retries=2)
        with self.assertRaises(TransientFailure):
            f.get("https://b.test/x")  # unreachable robots: stop, do not assume permission
        f, _ = self.fetcher(lambda u: Response(403, {}, b"") if u.endswith("robots.txt") else ok(),
                            respect_robots=True)
        self.assertEqual(f.get("https://c.test/x").status, 200)  # RFC 9309: 4xx robots = no rules

    def test_request_and_time_budgets(self):
        f, t = self.fetcher(lambda u: ok(), max_requests=2)
        f.get("https://a.test/1")
        f.get("https://a.test/2")
        with self.assertRaises(BudgetExhausted) as ctx:
            f.get("https://a.test/3")
        self.assertEqual((ctx.exception.reason, len(t.urls)), ("request_budget", 2))
        clock = [0.0]
        f, _ = self.fetcher(lambda u: ok(), max_runtime_s=10, clock=lambda: clock[0])
        f.get("https://a.test/1")
        clock[0] = 11
        with self.assertRaises(BudgetExhausted) as ctx:
            f.get("https://a.test/2")
        self.assertEqual(ctx.exception.reason, "time_budget")

    def test_same_host_spaced_after_response_other_hosts_parallel(self):
        stamps = {"a.test": [], "b.test": []}

        def handler(url):
            host = url.split("/")[2]
            stamps[host].append(("start", time.monotonic()))
            time.sleep(0.05)
            stamps[host].append(("end", time.monotonic()))
            return ok()

        interval = 0.3
        fa, _ = self.fetcher(handler, interval=interval, sleep=time.sleep)
        fb, _ = self.fetcher(handler, interval=interval, sleep=time.sleep)
        began = time.monotonic()
        threads = [threading.Thread(target=lambda f=f, h=h: [f.get(f"https://{h}/{i}") for i in range(3)])
                   for f, h in ((fa, "a.test"), (fb, "b.test"))]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        elapsed = time.monotonic() - began
        for host, events in stamps.items():
            ends = [t for kind, t in events if kind == "end"]
            starts = [t for kind, t in events if kind == "start"]
            gaps = [s - e for e, s in zip(ends, starts[1:])]
            self.assertTrue(all(g >= interval - 0.02 for g in gaps), (host, gaps))
        # A single shared queue would need about 2 x (2 x 0.3 + 3 x 0.05) = 1.5 s.
        self.assertLess(elapsed, 1.1)


if __name__ == "__main__":
    unittest.main()
