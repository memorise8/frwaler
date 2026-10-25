"""A detail URL that redirects to another host that cannot be used fails alone (cross_host_redirect)
instead of stopping the site (nidcd r0907: detail -> www.nih.gov, robots.txt ConnectTimeout). No network."""
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from crawler.enumeration.http import (AccessBlocked, Fetcher, PermanentFailure, Response, RobotsUnavailable,
                                      TransientFailure, TransportError, site_host_keys)
from tests.enumeration_fakes import Env, FakeBoard, FakeTransport, board_plan
from tests.test_enumeration_extensions import RecordingTransport

SITE = site_host_keys("https://a.test/list")
PAGE = Response(200, {"Content-Type": "text/html"}, b"<html><body><h1>ok</h1></body></html>")
NO_RULES = Response(404, {"Content-Type": "text/plain"}, b"")


def moved(to):
    return Response(302, {"Location": to}, b"")


class FetcherCrossHostTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def fetcher(self, routes):
        def handler(url):
            parts = urlsplit(url)
            answer = routes[(parts.hostname, parts.path)]
            return answer() if callable(answer) else answer
        transport = FakeTransport(handler)
        return Fetcher("s", transport, Path(self.tmp.name) / "gates", interval=0, sleep=lambda s: None,
                       retries=1), transport

    def routes(self, other_robots):
        return {("a.test", "/robots.txt"): NO_RULES, ("a.test", "/d/1"): moved("https://other.test/x"),
                ("a.test", "/d/2"): PAGE, ("other.test", "/robots.txt"): other_robots, ("other.test", "/x"): PAGE}

    def test_unreachable_robots_on_other_host_fails_only_that_url(self):
        f, transport = self.fetcher(self.routes(TransportError("ConnectTimeout")))
        f.foreign_redirect_item = SITE
        with self.assertRaises(TransientFailure) as ctx:  # resumable: a later run may reach it
            f.get("https://a.test/d/1")
        self.assertTrue(ctx.exception.reason.startswith("cross_host_redirect:robots_unavailable"), ctx.exception.reason)
        self.assertNotIn("https://other.test/x", transport.urls)  # never requested
        self.assertEqual(200, f.get("https://a.test/d/2").status)  # the site goes on

    def test_without_the_flag_the_site_stops_as_before(self):
        f, _ = self.fetcher(self.routes(TransportError("ConnectTimeout")))
        with self.assertRaises(RobotsUnavailable):
            f.get("https://a.test/d/1")

    def test_refusal_by_other_host_does_not_block_this_site(self):
        f, transport = self.fetcher(self.routes(Response(403, {"Content-Type": "text/html"}, b"no")))
        f.foreign_redirect_item = SITE
        with self.assertRaises(PermanentFailure) as ctx:
            f.get("https://a.test/d/1")
        self.assertTrue(ctx.exception.reason.startswith("cross_host_redirect:"), ctx.exception.reason)
        self.assertIsNone(f.blocked)
        self.assertEqual(200, f.get("https://a.test/d/2").status)
        self.assertNotIn("https://other.test/x", transport.urls)

    def test_usable_other_host_is_still_followed(self):
        f, _ = self.fetcher(self.routes(NO_RULES))
        f.foreign_redirect_item = SITE
        self.assertEqual(200, f.get("https://a.test/d/1").status)

    def test_www_variant_is_the_same_host(self):
        routes = {("a.test", "/robots.txt"): NO_RULES, ("a.test", "/d/1"): moved("https://www.a.test/d/1"),
                  ("www.a.test", "/robots.txt"): TransportError("ConnectTimeout")}
        f, _ = self.fetcher(routes)
        f.foreign_redirect_item = SITE
        with self.assertRaises(RobotsUnavailable):
            f.get("https://a.test/d/1")

    def test_site_host_reached_through_another_host_still_stops(self):
        # e.g. a second_step link on doi.test that lands on the site's own host, which refuses (review M2).
        routes = {("doi.test", "/robots.txt"): NO_RULES, ("doi.test", "/10.1/x"): moved("https://a.test/d/9"),
                  ("a.test", "/robots.txt"): NO_RULES, ("a.test", "/d/9"): Response(403, {}, b"no")}
        f, _ = self.fetcher(routes)
        f.foreign_redirect_item = SITE
        with self.assertRaises(AccessBlocked):
            f.get("https://doi.test/10.1/x")

    def test_own_host_refusal_still_stops(self):
        routes = {("a.test", "/robots.txt"): NO_RULES, ("a.test", "/d/1"): Response(403, {}, b"no")}
        f, _ = self.fetcher(routes)
        f.foreign_redirect_item = SITE
        with self.assertRaises(AccessBlocked):
            f.get("https://a.test/d/1")


class RunnerCrossHostTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def test_site_continues_and_ledgers_the_item(self):
        board = FakeBoard(5)

        def handle(method, url, data):
            parts = urlsplit(url)
            if parts.hostname == "other.test":
                return TransportError("ConnectTimeout")
            if parts.path == "/robots.txt":
                return NO_RULES
            if parts.path == "/view" and parts.query == "id=3":
                return moved("https://other.test/item/3")
            return board(url)
        result = self.env.run(board_plan(), None, transport=RecordingTransport(handle))
        self.assertNotIn("robots_unavailable", str(result.get("stop_reason")), result)
        self.assertEqual(4, len(self.env.docs()), result)
        saved, failed = self.env.store.item_counts("fake-site")
        self.assertEqual(4, saved)
        (key,) = failed
        self.assertTrue(key.startswith("failed_transient:transient:cross_host_redirect:robots_unavailable"), failed)
        self.assertEqual("complete_with_review", result["status"], result)  # a person sees the missed item

    def test_consecutive_cross_host_failures_stop_the_site(self):
        # The site moved to a host that refuses us: every detail fails -> stop, not "complete" (review H2).
        board = FakeBoard(8)

        def handle(method, url, data):
            parts = urlsplit(url)
            if parts.hostname == "other.test":
                return NO_RULES if parts.path == "/robots.txt" else Response(403, {}, b"no")
            if parts.path == "/robots.txt":
                return NO_RULES
            if parts.path == "/view":
                return moved("https://other.test" + parts.path + "?" + parts.query)
            return board(url)
        result = self.env.run(board_plan(), None, transport=RecordingTransport(handle))
        self.assertEqual("cross_host_redirect_consecutive", result["stop_reason"], result)
        self.assertEqual("access_blocked", result["status"], result)
        self.assertEqual(0, len(self.env.docs()))

    def test_consecutive_temporary_failures_stop_resumably(self):
        # robots.txt of the other host unreachable for every detail: resumable stop with a cooldown,
        # not a hold that needs a person (re-review NEW-1).
        board = FakeBoard(8)

        def handle(method, url, data):
            parts = urlsplit(url)
            if parts.hostname == "other.test":
                return TransportError("ConnectTimeout")
            if parts.path == "/robots.txt":
                return NO_RULES
            if parts.path == "/view":
                return moved("https://other.test" + parts.path + "?" + parts.query)
            return board(url)
        result = self.env.run(board_plan(), None, transport=RecordingTransport(handle))
        self.assertEqual(("incomplete", "cross_host_redirect_consecutive"), (result["status"], result["stop_reason"]), result)
        self.assertIn("cooldown_until", self.env.store.site("fake-site")["state"])

    def test_plan_limit_is_validated(self):
        from crawler.enumeration.plan import PlanError
        with self.assertRaises(PlanError):
            self.env.plan(board_plan(max_consecutive_cross_host_redirect=0))
        self.env.plan(board_plan(max_consecutive_cross_host_redirect=3))


if __name__ == "__main__":
    unittest.main()
