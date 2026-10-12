"""Third review round: review-needed completion status, identity rule
configuration errors, precheck match accounting."""
from __future__ import annotations

import csv
import io
import json
import os
import sys
import unittest
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from crawler import db_libertree  # noqa: E402
from crawler.enumeration.http import Response  # noqa: E402
from crawler.enumeration.report import site_report, write_report  # noqa: E402
from tests.enumeration_fakes import Env, FakeBoard, board_plan  # noqa: E402
from tests.test_enumeration_review import crawler_plan, generic_crawler_for, reference_db  # noqa: E402


def krivet_handler(ids, href="/v?pstNo={i}&a=3"):
    def handler(url):
        parts = urlsplit(url)
        if parts.path == "/robots.txt":
            return Response(404, {}, b"")
        if parts.path == "/list" and parse_qs(parts.query).get("page", ["1"]) == ["1"]:
            rows = "".join(f'<tr><td><a href="{href.format(i=i)}">T{i}</a></td></tr>' for i in ids)
        else:
            rows = ""
        return Response(200, {"Content-Type": "text/html"}, f"<html><body><table><tbody>{rows}</tbody></table></body></html>")
    return handler


def krivet_site(id_regex=r"pstNo=(\d+)"):
    parser = {"type": "html", "item_container": "tbody tr", "item_link": "a[href]"}
    if id_regex:
        parser["id_regex"] = id_regex
    site = board_plan(base_url="https://www.krivet.re.kr",
                      listing={"url": "https://www.krivet.re.kr/list", "total": None, "parser": parser})
    site["adapter"] = {"type": "list_only", **({"id_regex": id_regex} if id_regex else {})}
    return site


class ReviewStatusTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_collision_makes_complete_with_review_and_rerun_keeps_it(self):
        db_libertree.insert_document(self.env.conn, {"site_id": "fake-site", "post_number": "3",
                                                     "meta_url": "https://board.test/old/3", "title": "old"})
        result = self.env.run(board_plan(), FakeBoard(5))
        self.assertEqual((result["status"], result["stop_reason"]), ("complete_with_review", "identity_collisions:1"))
        site = self.env.store.site(self.env.site_id)
        self.assertEqual(site["status"], "complete_with_review")
        again = self.env.run(board_plan(), FakeBoard(5))
        self.assertEqual(again["stop_reason"], "already_complete_retry_only")
        self.assertEqual(self.env.store.site(self.env.site_id)["status"], "complete_with_review")
        out = self.env.root / "rep"
        write_report(self.env.store, [self.env.site_id], out, doc_conn=self.env.conn)
        row = next(csv.DictReader(io.StringIO((out / "completeness.csv").read_text())))
        self.assertEqual((row["status"], row["identity_collisions"]), ("complete_with_review", "1"))
        data = json.loads((out / "completeness.json").read_text())["sites"][0]
        self.assertIn("identity_collisions:1", data["warnings"])

    def test_cli_exit_codes(self):
        from crawler.enumeration.run import exit_code
        self.assertEqual(exit_code([{"status": "complete"}, {"status": "incomplete"}]), 0)
        self.assertEqual(exit_code([{"status": "complete"}, {"status": "complete_with_review"}]), 2)
        self.assertEqual(exit_code([{"status": "review_pending"}]), 2)
        self.assertEqual(exit_code([{"status": "complete_with_review"}, {"status": "failed_duplicates"}]), 1)
        self.assertEqual(exit_code([{"status": "config_error"}]), 1)


class IdentityRuleClassificationTest(unittest.TestCase):
    def test_configuration_errors_stop_the_site(self):
        env = Env("krivet-re-kr")
        try:
            # No pstNo rule configured: post_number would be the whole URL, which the
            # KRIVET rule rejects for every item.
            result = env.run(krivet_site(id_regex=None), krivet_handler(range(1, 8)))
            self.assertEqual(result["status"], "config_error", result)
            self.assertEqual(result["stop_reason"], "identity_rule_config:krivet_post_number_must_match_pstNo")
            self.assertEqual(env.docs(), [])
        finally:
            env.close()

    def test_review_rules_are_item_level_with_a_streak_stop(self):
        env = Env("krivet-re-kr")
        try:
            for i in range(1, 8):  # two stored rows per pstNo: legacy defect needing review
                for q in (f"pstNo={i}&a=1", f"a=2&pstNo={i}"):
                    env.conn.execute("INSERT INTO documents(site_id, post_number, meta_url, title) VALUES(?,?,?,?)",
                                     ("krivet-re-kr", str(i), f"https://www.krivet.re.kr/v?{q}", "x"))
            env.conn.commit()
            result = env.run(krivet_site(), krivet_handler(range(1, 8)))
            self.assertEqual((result["status"], result["stop_reason"]), ("review_pending", "identity_reviews_consecutive"))
            reviews = env.store._all("SELECT COUNT(*) AS n FROM enum_item WHERE reason LIKE 'identity_review:%'")
            self.assertEqual(reviews[0]["n"], 5)
        finally:
            env.close()


class PrecheckAccountingTest(unittest.TestCase):
    def test_listing_fallback_does_not_count_review_as_match(self):
        env = Env("krivet-re-kr")
        try:
            ref = reference_db([], site_id="krivet-re-kr")
            rows = [("1", "https://www.krivet.re.kr/v?pstNo=1&a=1"), ("1", "https://www.krivet.re.kr/v?a=2&pstNo=1")]
            # Newest rows use an old URL format without pstNo: no identity can be
            # rebuilt from them, so the precheck falls back to the listing page.
            rows += [(f"old{i}", f"https://www.krivet.re.kr/old?doc={i}") for i in range(5)]
            for post_number, url in rows:
                ref.execute("INSERT INTO documents(site_id, post_number, meta_url, title) VALUES(?,?,?,?)",
                            ("krivet-re-kr", post_number, url, "x"))
            ref.commit()
            crawler = generic_crawler_for(env, site_id="krivet-re-kr", base_url="https://www.krivet.re.kr",
                                          id_regex=None)
            site = crawler_plan(base_url="https://www.krivet.re.kr",
                                listing={"url": "https://www.krivet.re.kr/list", "parser": {"type": "adapter"}})
            result = env.run(site, krivet_handler([1], href="/v?pstNo={i}&a=9"), crawler=crawler,
                             reference_conn=ref, identity_required=True)
            identity = result["identity"]
            self.assertEqual(identity["source"], "listing_start_page")
            self.assertEqual((identity["sampled_reference"], identity["sampled_listing"], identity["sampled"]), (5, 1, 6))
            self.assertEqual((identity["compared"], identity["matched"]), (1, 0))  # a review is not a match
            self.assertEqual(result["status"], "config_error")
            ref.close()
        finally:
            env.close()

    def test_mismatch_reason_flags_possible_recompute_failure_for_list_field_identities(self):
        env = Env()
        try:
            from tests.test_enumeration_ledgers import FakeCustomCrawler, custom_plan
            ref = reference_db([(f"x{i}", f"https://board.test/view?id={i}") for i in range(1, 8)])
            result = env.run(custom_plan(), FakeBoard(7), crawler=FakeCustomCrawler(env.conn),
                             reference_conn=ref, identity_required=True)
            self.assertEqual(result["status"], "config_error", result)
            self.assertEqual(result["stop_reason"], "identity_mismatch:sample_recompute_may_be_wrong")
            self.assertTrue(result["identity"]["list_field_identity"])
            ref.close()
        finally:
            env.close()


if __name__ == "__main__":
    unittest.main()
