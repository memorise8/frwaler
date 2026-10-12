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


ROBOTS_TABLE = [
    # (robots.txt, path, allowed) - RFC 9309 verdicts, unchanged by the matcher rewrite.
    ("User-agent: *\nDisallow: /private", "/private/x", False),
    ("User-agent: *\nDisallow: /private", "/public", True),
    ("User-agent: *\nDisallow: /*.pdf$", "/a/b.pdf", False),
    ("User-agent: *\nDisallow: /*.pdf$", "/a/b.pdf?x=1", True),
    ("User-agent: *\nDisallow: /*.pdf$", "/a/b.PDF", True),
    ("User-agent: *\nDisallow: /folder\nAllow: /folder/", "/folder/page", True),
    ("User-agent: *\nDisallow: /folder\nAllow: /folder/", "/folder", False),
    ("User-agent: *\nAllow: /page\nDisallow: /page", "/page", True),
    ("User-agent: *\nDisallow: /*\nAllow: /$", "/", True),
    ("User-agent: *\nDisallow: /*\nAllow: /$", "/x", False),
    ("User-agent: *\nDisallow:", "/anything", True),
    ("User-agent: *\nDisallow: /", "/anything", False),
    ("User-agent: Googlebot\nDisallow: /\n\nUser-agent: *\nDisallow: /private", "/x", True),
    ("User-agent: Googlebot\nDisallow: /\n\nUser-agent: *\nDisallow: /private", "/private", False),
    ("User-agent: foo\nUser-agent: *\nDisallow: /x", "/x/1", False),
    ("User-agent: *\nDisallow: /a\n\nUser-agent: *\nDisallow: /b", "/b", False),
    ("User-agent: *\nDisallow: /a\n\nDisallow: /b", "/b", False),
    ("User-agent: * # all\nDisallow: /a # secret\n# Disallow: /b", "/a", False),
    ("User-agent: * # all\nDisallow: /a # secret\n# Disallow: /b", "/b", True),
    ("﻿User-agent: *\nDisallow: /x", "/x", False),
    ("User-agent: *\r\nDisallow: /x\r\n", "/x", False),
    ("User-agent: *\rDisallow: /x\r", "/x", False),
    ("USER-AGENT: *\nDISALLOW: /X", "/X", False),
    ("USER-AGENT: *\nDISALLOW: /X", "/x", True),
    ("User-agent: *\nDisallow: /%7Euser", "/~user/a", False),
    ("User-agent: *\nDisallow: /검색", "/%EA%B2%80%EC%83%89?q=1", False),
    ("User-agent: *\nDisallow: /%EA%B2%80", "/%ea%b2%80", False),
    ("User-agent: *\nDisallow: /*?", "/page?x=1", False),
    ("User-agent: *\nDisallow: /*?", "/page", True),
    ("User-agent: *\nDisallow: /*?*sort=", "/list?page=2&sort=asc", False),
    ("Disallow: /x\nUser-agent: *\nDisallow: /y", "/x", True),
    ("User-agent: a\nSitemap: https://x/s.xml\nUser-agent: *\nDisallow: /z", "/z", False),
    ("User-agent: * \nDisallow: /a", "/a", False),
    ("User-agent: Mozilla\nAllow: /\n\nUser-agent: *\nDisallow: /", "/x", True),
    ("User-agent: Chrome\nDisallow: /\n\nUser-agent: *\nDisallow:", "/x", True),
    ("User-agent: mozilla\nDisallow:\n\nUser-agent: *\nDisallow: /", "/x", True),
    ("User-agent: *\nDisallow: *", "/x", False),
    ("User-agent: *\nDisallow: /a$b", "/a$b", False),
    ("User-agent: *\nAllow: /a/*/c\nDisallow: /a/b", "/a/b/c", True),
    ("Disallow: /", "/x", True),
    ("User-agent:*\nDisallow:/x", "/x", False),
    # Not fixed by the RFC; the current verdicts are kept.
    ("User-agent: Mozilla/5.0\nDisallow: /\n\nUser-agent: *\nDisallow:", "/x", True),
    ("User-agent: *\nDisallow: private", "/private", False),
    ("User-agent: Mozi*\nDisallow: /\n\nUser-agent: *\nDisallow:", "/x", True),
]


def regex_reference(rule, target):
    """The previous regex translation of a robots rule, as the oracle for short inputs."""
    import re
    anchored = rule.endswith("$")
    body = rule[:-1] if anchored else rule
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body) + ("$" if anchored else "")
    return re.compile(regex, re.S).match(target) is not None


class RobotsMatcherTest(unittest.TestCase):
    """LOW-3: robots patterns are matched without regex backtracking, with the same verdicts."""

    def allowed(self, robots, path):
        from crawler.enumeration.http import DEFAULT_USER_AGENT, parse_robots
        return parse_robots(robots).allows("https://h.test" + path, DEFAULT_USER_AGENT)

    def test_rfc9309_table_unchanged(self):
        self.assertEqual(len(ROBOTS_TABLE), 44)
        for robots, path, expected in ROBOTS_TABLE:
            self.assertIs(self.allowed(robots, path), expected, (robots, path))

    def test_pathological_patterns_are_fast(self):
        import time
        from crawler.enumeration.http import DEFAULT_USER_AGENT, parse_robots
        long_query = "/board/view.do?" + "&".join(f"p{i}=v{i}" for i in range(25))
        cases = [("/*a*a*a*a*a*a*a*a*a*b$", "/" + "a" * 60, True),
                 ("/*a*a*a*a*a*a*a*a*a*b$", "/" + "a" * 2000, True),
                 ("/**********************a**********b$", "/" + "a" * 2000, True),
                 ("/*?*&*&*&*=x$", long_query, True), ("/*?*sort=*&*filter=", long_query, True),
                 ("/*/*/*/*/*.pdf$", long_query, True), ("/*?*&*&*&*&*&*=zz$", long_query, True),
                 ("/*a*a*a*a*b$", long_query, True)]
        for rule, path, expected in cases:
            rules = parse_robots("User-agent: *\nDisallow: " + rule)
            start = time.perf_counter()
            verdict = rules.allows("https://h.test" + path, DEFAULT_USER_AGENT)
            self.assertLess(time.perf_counter() - start, 0.1, rule)
            self.assertIs(verdict, expected, rule)

    def test_collapsed_wildcards_match_like_one(self):
        self.assertFalse(self.allowed("User-agent: *\nDisallow: /a***b$", "/axxb"))
        self.assertTrue(self.allowed("User-agent: *\nDisallow: /a***b$", "/axxbc"))
        self.assertFalse(self.allowed("User-agent: *\nDisallow: /a**\nAllow: /a", "/ab"))  # length 4 > 2

    def test_matches_regex_reference_on_random_rules(self):
        import random
        from crawler.enumeration.http import DEFAULT_USER_AGENT, parse_robots
        rng = random.Random(9309)
        for _ in range(3000):
            rule = "/" + "".join(rng.choice("ab*$/") for _ in range(rng.randint(0, 7)))
            path = "/" + "".join(rng.choice("ab$/") for _ in range(rng.randint(0, 9)))
            verdict = parse_robots("User-agent: *\nDisallow: " + rule).allows("https://h.test" + path,
                                                                              DEFAULT_USER_AGENT)
            self.assertEqual(verdict, not regex_reference(rule, path), (rule, path))


class ConsecutiveRobotsLimitTest(unittest.TestCase):
    """LOW-4: max_consecutive_robots_disallowed is validated; its stop is told apart from a
    listing URL that robots.txt itself denies."""

    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_limit_validated(self):
        from crawler.enumeration.plan import PlanError
        from tests.enumeration_fakes import board_plan
        for bad in (0, -1, 1.5, "5", True, None):
            with self.assertRaises(PlanError, msg=bad):
                self.env.plan(board_plan(max_consecutive_robots_disallowed=bad))
        self.assertEqual(self.env.plan(board_plan()).get("max_consecutive_robots_disallowed"), 5)
        self.assertEqual(self.env.plan(board_plan(max_consecutive_robots_disallowed=1))
                         .get("max_consecutive_robots_disallowed"), 1)

    def test_consecutive_stop_reason(self):
        from tests.enumeration_fakes import FakeBoard, board_plan
        board = FakeBoard(25, robots=b"User-agent: *\nDisallow: /view\n")
        result = self.env.run(board_plan(), board)
        self.assertEqual((result["status"], result["stop_reason"]),
                         ("access_blocked", "robots_disallowed_consecutive"))

    def test_limit_of_one_stops_at_first_denial(self):
        from tests.enumeration_fakes import FakeBoard, board_plan
        board = FakeBoard(25, robots=b"User-agent: *\nDisallow: /view?id=25$\n")
        result = self.env.run(board_plan(max_consecutive_robots_disallowed=1), board)
        self.assertEqual((result["status"], result["stop_reason"]),
                         ("access_blocked", "robots_disallowed_consecutive"))

    def test_listing_denial_keeps_its_reason(self):
        from tests.enumeration_fakes import FakeBoard, board_plan
        board = FakeBoard(25, robots=b"User-agent: *\nDisallow: /list\n")
        result = self.env.run(board_plan(), board)
        self.assertEqual((result["status"], result["stop_reason"]), ("access_blocked", "robots_disallowed"))


class CrawlDelayValueTest(unittest.TestCase):
    """LOW-5: '10s' / '1/10s' are read with their unit; a value that cannot be read in a group that
    applies to us stops the site (never read as no delay); Crawl-delay groups are chosen like the
    rule groups (whole product token or '*')."""

    def delay(self, robots):
        from crawler.enumeration.http import DEFAULT_USER_AGENT, crawl_delay
        return crawl_delay(robots, DEFAULT_USER_AGENT)

    def rate(self, robots):
        from crawler.enumeration.http import DEFAULT_USER_AGENT, parse_robots
        return parse_robots(robots).request_rate(DEFAULT_USER_AGENT)

    def test_second_suffix(self):
        self.assertEqual(self.delay("User-agent: *\nCrawl-delay: 10s\n"), 10)
        self.assertEqual(self.delay("User-agent: *\nCrawl-delay: 2.5 S\n"), 2.5)
        self.assertEqual(self.delay("User-agent: *\nCrawl-delay: 7\n"), 7)
        self.assertEqual(self.rate("User-agent: *\nRequest-rate: 1/10s\n"), 10)
        self.assertEqual(self.rate("User-agent: *\nRequest-rate: 1/10\n"), 10)
        self.assertEqual(self.rate("User-agent: *\nRequest-rate: 2/10\n"), 5)
        self.assertEqual(self.rate("User-agent: *\nRequest-rate: 1/2m\n"), 120)

    def test_unreadable_values_raise(self):
        for value in ("abc", "nan", "inf", "-5", "10ms", "1e3"):
            with self.assertRaises(ValueError, msg=value):
                self.delay(f"User-agent: *\nCrawl-delay: {value}\n")
        for value in ("1/10x", "0/10", "1/10s 0800-1700", "fast", "1/-1"):
            with self.assertRaises(ValueError, msg=value):
                self.rate(f"User-agent: *\nRequest-rate: {value}\n")

    def test_unreadable_values_of_other_groups_ignored(self):
        robots = "User-agent: Googlebot\nCrawl-delay: abc\nRequest-rate: soon\n\nUser-agent: *\nCrawl-delay: 3\n"
        self.assertEqual(self.delay(robots), 3)
        self.assertEqual(self.rate(robots), 0)
        self.assertEqual(self.delay("User-agent: *\nCrawl-delay:\n"), 0)

    def test_crawl_delay_group_selection_matches_rule_groups(self):
        self.assertEqual(self.delay("User-agent: a\nCrawl-delay: 99\n"), 0)
        self.assertEqual(self.delay("User-agent: Safari\nCrawl-delay: 400\n\nUser-agent: *\nCrawl-delay: 1\n"), 1)
        self.assertEqual(self.delay("User-agent: Mozilla/5.0 (compatible; Yeti/1.1)\nCrawl-delay: 99\n"), 0)
        self.assertEqual(self.delay("User-agent: mozilla\nCrawl-delay: 9\n\nUser-agent: *\nCrawl-delay: 1\n"), 9)
        self.assertEqual(self.delay("User-agent: *\nCrawl-delay: 9\n\nUser-agent: Mozilla\nCrawl-delay: 1\n"), 9)

    def test_fetcher_stops_on_unreadable_delay_and_waits_on_suffixed_one(self):
        from unittest import mock
        import delivery.health.request_gate as request_gate
        from crawler.enumeration import http as H
        from tests.enumeration_fakes import FakeTransport
        from tests.test_enumeration_fix2 import FakeClock
        env = Env()
        self.addCleanup(env.close)
        clock = FakeClock()  # the 10 s Crawl-delay is waited on a fake time line
        for patch in (mock.patch.object(request_gate, "time", clock), mock.patch.object(H, "time", clock)):
            patch.start()
            self.addCleanup(patch.stop)

        def fetcher(robots):
            def handler(url):
                if url.endswith("/robots.txt"):
                    return Response(200, {"Content-Type": "text/plain"}, robots)
                return Response(200, {"Content-Type": "text/html"}, b"<html><body><p>x</p></body></html>")
            t = FakeTransport(handler)
            return H.Fetcher(env.site_id, t, env.root / "gates", interval=0, sleep=lambda s: None,
                             holds=env.store), t
        for robots in (b"User-agent: *\nCrawl-delay: soon\n", b"User-agent: *\nRequest-rate: 1/10x\n"):
            f, t = fetcher(robots)
            with self.assertRaises(H.AccessBlocked) as ctx:
                f.get("https://x.org/a")
            self.assertEqual(ctx.exception.reason, "robots_unparseable")
            self.assertEqual(t.urls, ["https://x.org/robots.txt"])
        f, t = fetcher(b"User-agent: *\nRequest-rate: 1/1000s\n")
        with self.assertRaises(H.AccessBlocked) as ctx:
            f.get("https://x.org/a")
        self.assertEqual(ctx.exception.reason, "robots_crawl_delay_too_long")
        f, t = fetcher(b"User-agent: *\nCrawl-delay: 10s\n")
        f.get("https://x.org/a")
        self.assertEqual(f.timing_summary()["hosts"]["x.org"]["crawl_delay_s"], 10)


if __name__ == "__main__":
    unittest.main()
