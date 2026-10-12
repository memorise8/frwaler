"""Fixes after the fourth independent review (fake transports only, no network)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from crawler.enumeration.http import Response  # noqa: E402
from tests.enumeration_fakes import Env  # noqa: E402


class OverallTotalFailureTest(unittest.TestCase):
    """MEDIUM-1: the unfiltered total request failing with a 4xx/parse error leaves the site
    complete_unverified (overall_total_unavailable), not incomplete forever; refusals and robots
    denial of that request still stop the site."""

    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def handler(self, unfiltered):
        from tests.test_enumeration_date_range import FakeSearch
        from tests.test_enumeration_fix2 import DateRangeBoundaryTest
        search = FakeSearch(DateRangeBoundaryTest.items())

        def handler(url):
            if "/search" in url and "from=" not in url:
                return unfiltered(url)
            return search(url)
        return handler

    def run_plan(self, handler, **over):
        from tests.test_enumeration_date_range import plan
        return self.env.run(plan(**over), handler)

    def test_overall_total_4xx_is_unverified_not_incomplete(self):
        handler = self.handler(lambda url: Response(400, {"Content-Type": "text/html"},
                                                    b"<html><body>date filter required</body></html>"))
        result = self.run_plan(handler)
        self.assertEqual((result["status"], result["stop_reason"]),
                         ("complete_unverified", "overall_total_unavailable"), result)
        # A rerun keeps the completed status (retry pass only), never turns into listing_failed.
        self.assertEqual(self.run_plan(handler)["status"], "complete_unverified")
        kinds = [e["kind"] for e in self.env.store.events(self.env.site_id)]
        self.assertIn("overall_total_unavailable", kinds)
        event = [e for e in self.env.store.events(self.env.site_id) if e["kind"] == "overall_total_unavailable"][-1]
        self.assertEqual(event["detail"]["status"], 400)
        self.assertEqual(len(self.env.docs()), 9)

    def test_overall_total_parse_error_is_unverified(self):
        handler = self.handler(lambda url: Response(200, {"Content-Type": "application/json"}, b"{broken"))
        result = self.run_plan(handler)
        self.assertEqual((result["status"], result["stop_reason"]),
                         ("complete_unverified", "overall_total_unavailable"), result)

    def test_overall_total_refusal_still_blocks(self):
        handler = self.handler(lambda url: Response(403, {"Content-Type": "text/html"}, b"<html>no</html>"))
        result = self.run_plan(handler)
        self.assertEqual(result["status"], "access_blocked", result)

    def test_overall_total_rate_limit_is_not_unverified(self):
        handler = self.handler(lambda url: Response(429, {"Content-Type": "text/html", "Retry-After": "3600"},
                                                    b"<html>slow down</html>"))
        result = self.run_plan(handler)
        self.assertNotIn(result["status"], ("complete", "complete_unverified"), result)

    def test_overall_total_robots_disallowed_still_blocks(self):
        from tests.test_enumeration_date_range import FakeSearch
        from tests.test_enumeration_fix2 import DateRangeBoundaryTest
        search = FakeSearch(DateRangeBoundaryTest.items())

        def handler(url):
            if url.endswith("/robots.txt"):
                return Response(200, {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /search?page=\n")
            return search(url)
        result = self.run_plan(handler)
        self.assertEqual((result["status"], result["stop_reason"]), ("access_blocked", "robots_disallowed"), result)


if __name__ == "__main__":
    unittest.main()
