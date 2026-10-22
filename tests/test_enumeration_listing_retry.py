"""Listing transient retry (plan listing_retry): wait, retry the same listing request, then stop.
Fake transports only; waits go through an injected sleep/clock (nothing sleeps)."""
from __future__ import annotations

import os
import signal
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from crawler.enumeration import http as H  # noqa: E402
from crawler.enumeration import run as R  # noqa: E402
from crawler.enumeration.http import Response, TransportError  # noqa: E402
from crawler.enumeration.plan import PlanError  # noqa: E402
from enumeration_fakes import Env, FakeBoard, board_plan  # noqa: E402


class Clock:
    """Fake monotonic clock; sleep() advances it and records each slice."""

    def __init__(self):
        self.t = 1000.0
        self.slices = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slices.append(s)
        self.t += s

    @property
    def paused(self):
        return sum(s for s in self.slices if s > 0)


def failing_lists(board, failures, response=lambda: Response(503, {}, b"")):
    """Handler: the first ``failures`` listing requests fail, then the board answers."""
    state = {"left": failures}

    def handler(url):
        if "/list" in url and state["left"] > 0:
            state["left"] -= 1
            return response()
        return board(url)
    return handler


class ListingRetryTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.clock = Clock()
        self.cancel = threading.Event()
        self.pauses = []
        original = H.Fetcher.pause

        def spy(fetcher, seconds):
            self.pauses.append(seconds)
            return original(fetcher, seconds)
        patch = mock.patch.object(H.Fetcher, "pause", spy)
        patch.start()
        self.addCleanup(patch.stop)

    def run_site(self, handler, **over):
        return self.env.run(board_plan(**over), handler, clock=self.clock, sleep=self.clock.sleep, cancel=self.cancel)

    def retries(self):
        return self.env.store.events(self.env.site_id, "listing_transient_retry")

    def test_retry_then_succeed(self):
        # 3 Fetcher tries per listing attempt: two attempts fail, the third gets the page.
        result = self.run_site(failing_lists(FakeBoard(25), 6))
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(len(self.env.docs()), 25)
        events = self.retries()
        self.assertEqual([e["detail"]["attempt"] for e in events], [1, 2])
        self.assertEqual(events[0]["detail"]["reason"], "http_503")
        self.assertEqual(events[0]["detail"]["wait_s"], 300)
        self.assertEqual(self.clock.paused, 600)  # default wait_s 300, twice
        self.assertLessEqual(max(self.clock.slices), H.PAUSE_SLICE_S)  # sliced: a cancel is seen quickly

    def test_retry_exhausted_ends_listing_transient(self):
        result = self.run_site(failing_lists(FakeBoard(25), 10 ** 6), listing_retry={"wait_s": 60, "max": 2})
        self.assertEqual((result["status"], result["stop_reason"]), ("incomplete", "listing_transient:http_503"))
        self.assertEqual(len(self.retries()), 2)
        self.assertEqual(self.clock.paused, 120)
        self.assertEqual(len(result["transport"].urls), 1 + 3 * 3)  # robots + 3 attempts x 3 Fetcher tries
        self.assertEqual(self.env.store.site(self.env.site_id)["stop_reason"], "listing_transient:http_503")

    def test_transport_error_is_retried(self):
        handler = failing_lists(FakeBoard(5), 3, lambda: TransportError("ConnectionError"))
        result = self.run_site(handler, listing_retry={"wait_s": 10, "max": 1})
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(self.retries()[0]["detail"]["reason"], "transport:ConnectionError")

    def test_max_zero_disables_the_retry(self):
        result = self.run_site(failing_lists(FakeBoard(5), 3), listing_retry={"max": 0})
        self.assertEqual(result["stop_reason"], "listing_transient:http_503")
        self.assertEqual((self.retries(), self.clock.paused), ([], 0))

    def assert_not_retried(self, result):
        self.assertEqual(self.retries(), [])
        self.assertEqual(self.pauses, [])  # (the Fetcher's own 429 backoff may still sleep)

    def test_403_is_not_retried(self):
        result = self.run_site(failing_lists(FakeBoard(5), 10 ** 6, lambda: Response(403, {}, b"no")))
        self.assertEqual((result["status"], result["stop_reason"]), ("access_blocked", "access_refused"))
        self.assert_not_retried(result)

    def test_429_is_not_retried(self):
        # Persistent 429: the Fetcher's rate-limit stop (cooldown), never a listing wait.
        result = self.run_site(failing_lists(FakeBoard(5), 10 ** 6, lambda: Response(429, {}, b"")))
        self.assertEqual((result["status"], result["stop_reason"]), ("incomplete", "rate_limited_cooldown"))
        self.assert_not_retried(result)

    def test_429_below_the_rate_limit_stop_is_not_retried_either(self):
        # retries=2 < rate_limit_stop=3: a TransientFailure(429), still never waited out here.
        result = self.run_site(failing_lists(FakeBoard(5), 10 ** 6, lambda: Response(429, {}, b"")), retries=2)
        self.assertEqual((result["status"], result["stop_reason"]), ("incomplete", "listing_transient:rate_limited"))
        self.assert_not_retried(result)

    def test_404_is_not_retried(self):
        result = self.run_site(failing_lists(FakeBoard(5), 10 ** 6, lambda: Response(404, {}, b"")))
        self.assertNotIn("listing_transient", str(result["stop_reason"]))
        self.assert_not_retried(result)

    def test_robots_refusal_and_unavailable_robots_are_not_retried(self):
        result = self.run_site(FakeBoard(5, robots=b"User-agent: *\nDisallow: /list\n"))
        self.assertEqual((result["status"], result["stop_reason"]), ("access_blocked", "robots_disallowed"))
        self.assert_not_retried(result)
        env2 = Env()
        self.addCleanup(env2.close)
        board = FakeBoard(5)
        result = env2.run(board_plan(), lambda u: Response(503, {}, b"") if u.endswith("/robots.txt") else board(u),
                          clock=self.clock, sleep=self.clock.sleep, cancel=self.cancel)
        self.assertEqual(result["stop_reason"], "robots_unavailable:http_503")
        self.assertEqual(env2.store.events(env2.site_id, "listing_transient_retry"), [])
        self.assertEqual(self.pauses, [])

    def test_waits_stay_within_the_time_budget(self):
        started = self.clock.t
        result = self.run_site(failing_lists(FakeBoard(5), 10 ** 6), max_runtime_s=700)
        # 0 -> 300 -> 600; a third 300 s wait would reach 900 >= 700: not started. The run ends with the
        # original failure, never time_budget (an auto_continue chain must stop, not start again).
        self.assertEqual((result["status"], result["stop_reason"]), ("incomplete", "listing_transient:http_503"))
        self.assertEqual(len(self.retries()), 3)  # the third retry was announced, its wait refused
        self.assertEqual(len(self.env.store.events(self.env.site_id, "listing_transient_retry_no_budget")), 1)
        self.assertEqual(self.clock.t - started, 600)
        self.assertLess(self.clock.t - started, 700)

    def test_redirect_policy_refusals_get_no_wait(self):
        from types import SimpleNamespace
        from crawler.enumeration.http import TransientFailure
        from crawler.enumeration.strategies import listing_request
        plan = self.env.plan(board_plan())
        ctx = SimpleNamespace(plan=plan, store=self.env.store, site_id=self.env.site_id,
                              fetcher=SimpleNamespace(pause=lambda s: self.pauses.append(s)))

        def call():
            raise TransientFailure("redirect_invalid_host", "https://board.test/x")
        with self.assertRaises(TransientFailure) as caught:
            listing_request(ctx, call, "https://board.test/list")
        self.assertEqual(caught.exception.reason, "redirect_invalid_host")
        self.assert_not_retried(None)

    def test_cancel_ends_the_wait(self):
        def sleep(s):
            self.clock.sleep(s)
            if s > 0:
                self.cancel.set()  # the runner's cancel arrives during the wait
        result = self.env.run(board_plan(), failing_lists(FakeBoard(5), 10 ** 6), clock=self.clock, sleep=sleep,
                              cancel=self.cancel)
        self.assertEqual((result["status"], result["stop_reason"]), ("incomplete", "cancelled"))
        self.assertEqual(self.clock.paused, H.PAUSE_SLICE_S)  # one slice, not the whole 300 s
        self.assertEqual(self.env.store.site(self.env.site_id)["status"], "incomplete")

    def test_plan_validates_listing_retry(self):
        for bad in ({"wait_s": -1}, {"wait_s": 4000}, {"max": 11}, {"max": "3"}, {"max": True}, {"other": 1}, [], 5):
            with self.assertRaises(PlanError, msg=bad):
                self.env.plan(board_plan(listing_retry=bad))
        self.assertEqual(self.env.plan(board_plan()).get("listing_retry"), {"wait_s": 300, "max": 3})
        self.assertEqual(self.env.plan(board_plan(listing_retry={"wait_s": 30, "max": 1})).get("listing_retry"),
                         {"wait_s": 30, "max": 1})


class SigtermTest(unittest.TestCase):
    def tearDown(self):
        H.CANCEL.clear()

    def test_sigterm_during_a_wait_only_sets_cancel(self):
        H.WAITING[0] += 1
        try:
            with mock.patch.object(R.os, "kill") as kill, mock.patch.object(R.signal, "signal") as sig:
                R._on_sigterm(signal.SIGTERM, None)
            self.assertTrue(H.CANCEL.is_set())
            kill.assert_not_called()
            sig.assert_not_called()
        finally:
            H.WAITING[0] -= 1

    def test_sigterm_outside_a_wait_keeps_the_default_action(self):
        with mock.patch.object(R.os, "kill") as kill, mock.patch.object(R.signal, "signal") as sig:
            R._on_sigterm(signal.SIGTERM, None)
        sig.assert_called_once_with(signal.SIGTERM, signal.SIG_DFL)
        kill.assert_called_once_with(os.getpid(), signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
