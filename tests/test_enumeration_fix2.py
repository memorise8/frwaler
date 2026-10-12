"""Request-policy fixes after the first real-site runs (fake transports only, no network)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import delivery.health.request_gate as request_gate  # noqa: E402
from crawler.enumeration import http as H  # noqa: E402
from crawler.enumeration.http import Fetcher, Response  # noqa: E402
from tests.enumeration_fakes import FakeTransport  # noqa: E402


class FakeClock:
    """Wall clock + monotonic clock + sleep sharing one fake time line."""

    def __init__(self, start=1_000_000.0):
        self.t = start

    def time(self):
        return self.t

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(0.0, seconds)


def ok(body=b"<html><body><p>x</p></body></html>", **headers):
    return Response(200, {"Content-Type": "text/html", **headers}, body)


class CrawlDelayTest(unittest.TestCase):
    """HIGH-1: the gap between the end of one response and the next request is >= Crawl-delay."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        patches = [mock.patch.object(request_gate, "time", self.clock), mock.patch.object(H, "time", self.clock)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def run_pages(self, delay, n=3, interval=1.5):
        starts = []

        def handler(url):
            if url.endswith("/robots.txt"):
                return Response(200, {"Content-Type": "text/plain"}, f"User-agent: *\nCrawl-delay: {delay}\n".encode())
            starts.append(self.clock.t)
            self.clock.t += 0.5  # response time
            return ok()

        f = Fetcher("s", FakeTransport(handler), Path(self.tmp.name) / "gates", interval=interval,
                    clock=self.clock.monotonic, sleep=self.clock.sleep)
        for i in range(n):
            f.get(f"https://example.org/p{i}")
        # gap = next start - previous end (previous start + 0.5)
        return [round(b - (a + 0.5), 3) for a, b in zip(starts, starts[1:])]

    def test_crawl_delay_above_gate_ceiling_is_fully_respected(self):
        gaps = self.run_pages(20)
        self.assertTrue(gaps and all(g >= 20 for g in gaps), gaps)
        self.assertTrue(all(g < 21 for g in gaps), gaps)  # and not doubled

    def test_crawl_delay_under_ceiling_unchanged(self):
        gaps = self.run_pages(5)
        self.assertTrue(all(5 <= g < 5.5 for g in gaps), gaps)

    def test_fractional_crawl_delay_above_ceiling(self):
        gaps = self.run_pages(12.5)
        self.assertTrue(all(12.5 <= g < 13 for g in gaps), gaps)


UA = H.DEFAULT_USER_AGENT


class RobotsMatcherTest(unittest.TestCase):
    """HIGH-2: RFC 9309 matching (groups, longest match, Allow on ties, '*' and '$', %-normalisation)."""

    def allowed(self, robots, url, ua=UA):
        return H.parse_robots(robots).allows(url, ua)

    def test_wildcard_and_end_anchor(self):
        robots = "User-agent: *\nDisallow: /*?page=\nDisallow: /search/*.php$\n"
        self.assertFalse(self.allowed(robots, "https://x.org/list?page=2"))
        self.assertTrue(self.allowed(robots, "https://x.org/list"))
        self.assertFalse(self.allowed(robots, "https://x.org/search/a/b.php"))
        self.assertTrue(self.allowed(robots, "https://x.org/search/a/b.php?x=1"))
        self.assertTrue(self.allowed(robots, "https://x.org/search/a/b.phpx"))

    def test_longest_match_wins_regardless_of_order(self):
        for robots in ("User-agent: *\nDisallow: /\nAllow: /public\n",
                       "User-agent: *\nAllow: /public\nDisallow: /\n"):
            self.assertTrue(self.allowed(robots, "https://x.org/public/x"), robots)
            self.assertFalse(self.allowed(robots, "https://x.org/other"), robots)
        robots = "User-agent: *\nAllow: /a\nDisallow: /a/b\n"
        self.assertFalse(self.allowed(robots, "https://x.org/a/b/c"))
        self.assertTrue(self.allowed(robots, "https://x.org/a/c"))

    def test_equal_length_allow_wins_and_empty_disallow_allows(self):
        self.assertTrue(self.allowed("User-agent: *\nDisallow: /page\nAllow: /page\n", "https://x.org/page/1"))
        self.assertTrue(self.allowed("User-agent: *\nDisallow:\n", "https://x.org/anything"))
        self.assertFalse(self.allowed("User-agent: *\nDisallow: /\n", "https://x.org/"))
        self.assertFalse(self.allowed("User-agent: *\nDisallow: /\n", "https://x.org"))

    def test_group_selection(self):
        robots = ("User-agent: Googlebot\nDisallow: /\n\n"
                  "User-agent: *\nDisallow: /private\n")
        self.assertTrue(self.allowed(robots, "https://x.org/x"))
        self.assertFalse(self.allowed(robots, "https://x.org/private/1"))
        # A group naming a product token of our user agent replaces the '*' group.
        robots = "User-agent: *\nDisallow: /\n\nUser-agent: Chrome\nDisallow: /c\n"
        self.assertTrue(self.allowed(robots, "https://x.org/x"))
        self.assertFalse(self.allowed(robots, "https://x.org/c"))
        # Several user-agent lines share one group; groups for the same agent merge.
        robots = ("User-agent: foo\nUser-agent: *\nDisallow: /a\n\n"
                  "user-agent: *\ndisallow: /b\n")
        self.assertFalse(self.allowed(robots, "https://x.org/a"))
        self.assertFalse(self.allowed(robots, "https://x.org/b"))
        # Rules before any user-agent line belong to no group.
        self.assertTrue(self.allowed("Disallow: /\nUser-agent: *\nDisallow: /z\n", "https://x.org/a"))
        self.assertTrue(self.allowed("", "https://x.org/a"))

    def test_percent_encoding_normalisation(self):
        robots = "User-agent: *\nDisallow: /caf%C3%A9\nDisallow: /a%7Eb\nDisallow: /é\n"
        self.assertFalse(self.allowed(robots, "https://x.org/café/1"))
        self.assertFalse(self.allowed(robots, "https://x.org/caf%c3%a9/1"))
        self.assertFalse(self.allowed(robots, "https://x.org/a~b"))
        self.assertFalse(self.allowed(robots, "https://x.org/%C3%A9"))
        # A reserved character stays encoded: %2F is not '/'.
        self.assertTrue(self.allowed("User-agent: *\nDisallow: /a/b\n", "https://x.org/a%2Fb"))

    def test_bom_and_comments(self):
        robots = "﻿User-agent: * # all\nDisallow: /x # no\n"
        self.assertFalse(self.allowed(robots, "https://x.org/x"))


class RobotsFetcherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def fetcher(self, handler, **kw):
        kw.setdefault("interval", 0)
        kw.setdefault("sleep", lambda s: None)
        t = FakeTransport(handler)
        return Fetcher("s", t, Path(self.tmp.name) / "gates", **kw), t

    def test_allow_after_disallow_all(self):
        def handler(url):
            if url.endswith("/robots.txt"):
                return Response(200, {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /\nAllow: /public\n")
            return ok()
        f, _ = self.fetcher(handler)
        self.assertEqual(f.get("https://x.org/public/x").status, 200)
        with self.assertRaises(H.AccessBlocked) as ctx:
            f.get("https://x.org/other")
        self.assertEqual(ctx.exception.reason, "robots_disallowed")

    def test_unparseable_robots_blocks(self):
        def handler(url):
            if url.endswith("/robots.txt"):
                return Response(200, {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /x\n")
            return ok()
        f, t = self.fetcher(handler)
        with mock.patch.object(H, "parse_robots", side_effect=ValueError("boom")):
            with self.assertRaises(H.AccessBlocked) as ctx:
                f.get("https://x.org/a")
        self.assertEqual(ctx.exception.reason, "robots_unparseable")
        self.assertEqual(t.urls, ["https://x.org/robots.txt"])

    def test_redirected_robots_crawl_delay_keyed_to_origin_host(self):
        def handler(url):
            if url == "https://a.test/robots.txt":
                return Response(301, {"Location": "https://b.test/robots.txt"}, b"")
            if url == "https://b.test/robots.txt":
                return Response(200, {"Content-Type": "text/plain"}, b"User-agent: *\nCrawl-delay: 4\n")
            return ok()
        f, _ = self.fetcher(handler)
        f.get("https://a.test/x")
        self.assertEqual(f._host_delay.get("a.test"), 4)
        self.assertNotIn("b.test", f._host_delay)


class RobotsUnavailableTest(unittest.TestCase):
    """MEDIUM-3: an unreachable robots.txt stops the site and is not re-requested for every URL."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()

    def tearDown(self):
        self.tmp.cleanup()

    def test_failure_cached_then_retried_after_ttl(self):
        def handler(url):
            return Response(503, {}, b"") if url.endswith("/robots.txt") else ok()
        t = FakeTransport(handler)
        f = Fetcher("s", t, Path(self.tmp.name) / "gates", interval=0, retries=3, sleep=lambda s: None,
                    clock=self.clock.monotonic)
        for i in range(3):
            with self.assertRaises(H.RobotsUnavailable) as ctx:
                f.get(f"https://x.org/p{i}")
            self.assertEqual(ctx.exception.reason, "robots_unavailable:http_503")
        self.assertEqual(t.urls, ["https://x.org/robots.txt"] * 3)  # one robots fetch (3 tries), no page
        self.clock.t += H.ROBOTS_RETRY_S + 1
        with self.assertRaises(H.RobotsUnavailable):
            f.get("https://x.org/p9")
        self.assertEqual(len(t.urls), 6)

    def test_runner_stops_incomplete_without_item_requests(self):
        from tests.enumeration_fakes import Env, FakeBoard, board_plan
        env = Env()
        self.addCleanup(env.close)
        board = FakeBoard(25)

        def handler(url):
            return Response(503, {}, b"") if url.endswith("/robots.txt") else board(url)
        result = env.run(board_plan(), handler)
        self.assertEqual((result["status"], result["stop_reason"]), ("incomplete", "robots_unavailable:http_503"))
        urls = result["transport"].urls
        self.assertTrue(urls and all(u.endswith("/robots.txt") for u in urls), urls)
        self.assertEqual(len(urls), 3)
        self.assertIsNone(env.store.host_hold("board.test"))  # not a refusal: no host hold
        self.assertEqual(env.store.events(env.site_id, "robots_unavailable")[-1]["detail"]["reason"],
                         "robots_unavailable:http_503")

    def test_file_pass_stops_without_attempt(self):
        from crawler.enumeration.files import FilePass
        from tests.enumeration_fakes import Env
        env = Env()
        self.addCleanup(env.close)
        env.store.enqueue_file(env.site_id, 1, "https://cdn.test/a.pdf")
        t = FakeTransport(lambda u: Response(503, {}, b""))
        f = Fetcher(env.site_id, t, env.root / "gates", interval=0, sleep=lambda s: None, holds=env.store)
        stats = FilePass(env.store, f, blob_root=env.root / "blobs").run(env.site_id)
        self.assertEqual(stats["stopped"], "robots_unavailable:http_503")
        row = env.store.files(env.site_id)[0]
        self.assertEqual((row["status"], row["attempts"]), ("pending", 0))


if __name__ == "__main__":
    unittest.main()
