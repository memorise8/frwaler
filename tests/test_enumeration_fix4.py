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


class RobotsItemReviewTest(unittest.TestCase):
    """MEDIUM-2: item-level robots denials are counted in the report and warnings and, above
    max_robots_disallowed_ratio (default 0.0), turn complete into complete_with_review."""

    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def run_board(self, robots, **over):
        from tests.enumeration_fakes import FakeBoard, board_plan
        return self.env.run(board_plan(**over), FakeBoard(25, robots=robots))

    def report(self):
        from crawler.enumeration.report import site_report
        return site_report(self.env.store, self.env.site_id, self.env.conn)

    def test_one_denied_item_needs_review_by_default(self):
        result = self.run_board(b"User-agent: *\nDisallow: /view?id=5$\n")
        self.assertEqual((result["status"], result["stop_reason"]),
                         ("complete_with_review", "robots_disallowed:1"), result)
        report = self.report()
        self.assertEqual(report["robots_disallowed"], 1)
        self.assertIn("robots_disallowed:1", report["warnings"])

    def test_ratio_tolerates_and_still_reports(self):
        result = self.run_board(b"User-agent: *\nDisallow: /view?id=5$\n", max_robots_disallowed_ratio=0.1)
        self.assertEqual(result["status"], "complete", result)
        report = self.report()
        self.assertEqual(report["robots_disallowed"], 1)
        self.assertEqual(report["warnings"], ["robots_disallowed:1"])

    def test_ratio_exceeded(self):
        robots = b"User-agent: *\n" + b"".join(b"Disallow: /view?id=%d$\n" % i for i in (2, 5, 8, 11))
        result = self.run_board(robots, max_robots_disallowed_ratio=0.1)
        self.assertEqual((result["status"], result["stop_reason"]),
                         ("complete_with_review", "robots_disallowed:4"), result)

    def test_no_denial_stays_complete(self):
        result = self.run_board(b"User-agent: *\nDisallow: /private\n")
        self.assertEqual(result["status"], "complete", result)
        report = self.report()
        self.assertEqual(report["robots_disallowed"], 0)
        self.assertEqual(report["warnings"], [])

    def test_ratio_validated(self):
        from crawler.enumeration.plan import PlanError
        from tests.enumeration_fakes import board_plan
        for bad in (-0.1, 1, 1.5, "0.1", None, True):
            with self.assertRaises(PlanError, msg=bad):
                self.env.plan(board_plan(max_robots_disallowed_ratio=bad))
        self.assertEqual(self.env.plan(board_plan(max_robots_disallowed_ratio=0)).get("max_robots_disallowed_ratio"), 0)

    def test_csv_report_has_the_count(self):
        import csv
        from crawler.enumeration.report import write_report
        self.run_board(b"User-agent: *\nDisallow: /view?id=5$\n")
        write_report(self.env.store, [self.env.site_id], self.env.root / "out", self.env.conn)
        with open(self.env.root / "out" / "completeness.csv", encoding="utf-8") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row["robots_disallowed"], "1")


if __name__ == "__main__":
    unittest.main()
