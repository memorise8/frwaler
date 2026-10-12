"""Contract sites driven through the real SiteRunner.run() with a fake board (no network)."""
import unittest
from unittest.mock import patch

from crawler.base_crawler import BaseCrawler
from tests.enumeration_fakes import Env, FakeBoard


def site(**over):
    out = {"strategy": "last_page_backward", "request_interval_s": 0, "base_url": "https://board.test",
           "listing": {"url": "https://board.test/list", "page_param": "page", "start_page": 1,
                       "parser": {"type": "adapter", "listing_marker": "tbody"}},
           "adapter": {"type": "crawler", "contract_version": 1, "list_method": "listing",
                       "detail_method": "detail"}}
    out.update(over)
    return out


def crawler(env, detail):
    class Board(BaseCrawler):
        site_id = "fake-site"
        site_name = "fake-site"
        base_url = "https://board.test"

        def crawl(self, limit=None):
            pass

        def listing(self, html):
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            return [{"url": a["href"], "external_id": a["href"].split("=")[1], "title": a.get_text()}
                    for a in soup.select("tbody a")]

    c = Board(env.conn, 0)
    c.detail = detail
    return c


def good(html, item):
    return {"external_id": item["external_id"], "url": item["url"], "title": item["title"]}


class ContractRunnerTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def run_site(self, detail, plan=None, n=25):
        board = FakeBoard(n, page_size=10)
        return self.env.run(plan or site(), board, crawler=crawler(self.env, detail))

    def test_all_items_raising_stops_site(self):
        def detail(html, item):
            return {"external_id": item["slug"]}  # binding/field mismatch -> KeyError
        result = self.run_site(detail)
        self.assertEqual("config_error", result["status"])
        self.assertTrue(result["stop_reason"].startswith("adapter_contract_failures_consecutive:parse_error:KeyError"),
                        result["stop_reason"])
        self.assertEqual(5, result["stats"]["failed_permanent"])

    def test_all_items_none_stops_site(self):
        result = self.run_site(lambda html, item: None)
        self.assertEqual(("config_error", "adapter_contract_failures_consecutive:empty_item"),
                         (result["status"], result["stop_reason"]))

    def test_consecutive_limit_from_plan(self):
        result = self.run_site(lambda html, item: None, site(max_consecutive_contract_failures=2))
        self.assertEqual("config_error", result["status"])
        self.assertEqual(2, result["stats"]["failed_permanent"])

    def test_scattered_failures_never_plain_complete(self):
        def detail(html, item):
            if int(item["external_id"]) % 4 == 0:
                raise KeyError("slug")
            return good(html, item)
        result = self.run_site(detail)
        self.assertEqual("complete_with_review", result["status"])
        self.assertIn("adapter_contract_failures:6", result["stop_reason"])

    def test_ratio_threshold_from_plan(self):
        def detail(html, item):
            if int(item["external_id"]) % 4 == 0:
                raise KeyError("slug")
            return good(html, item)
        result = self.run_site(detail, site(max_contract_failure_ratio=0.5))
        self.assertEqual("complete", result["status"])

    def test_all_good_is_complete(self):
        result = self.run_site(good)
        self.assertEqual("complete", result["status"])
        self.assertEqual(25, result["stats"]["saved_new"])

    def test_schema_error_stops_without_checkpoint(self):
        original = self.env.store.update_segment
        with patch.object(self.env.store, "mark_listed", side_effect=AssertionError("mark_listed")) as listed, \
                patch.object(self.env.store, "update_segment", side_effect=original) as segment:
            result = self.run_site(lambda html, item: [])
        self.assertEqual(("config_error", "adapter_contract:paper_not_dict"), (result["status"], result["stop_reason"]))
        listed.assert_not_called()
        # Only the strategy's last-page discovery record is written; the runner's
        # per-batch checkpoint (cursor advanced past processed items) never happens.
        cursors = [c.kwargs["cursor"] for c in segment.call_args_list if "cursor" in c.kwargs]
        self.assertEqual(1, len(cursors), segment.call_args_list)
        self.assertEqual(cursors[0]["last_page"], cursors[0]["next_page"])
        self.assertEqual("config_error", self.env.store.site("fake-site")["status"])
        self.assertEqual(0, result["stats"]["failed_permanent"])

    def test_data_contract_error_is_ledgered_for_review(self):
        def detail(html, item):
            if int(item["external_id"]) % 5 == 0:
                return {"url": item["url"], "title": "no identity"}
            return good(html, item)
        result = self.run_site(detail)
        self.assertEqual("complete_with_review", result["status"])
        # Missing identity is identity_missing review (re-review MEDIUM-2), not a ratio failure.
        self.assertEqual("identity_missing:5", result["stop_reason"])
        _, failed = self.env.store.item_counts("fake-site")
        self.assertEqual(5, failed["failed_permanent:adapter_contract:paper_identity_missing"])
        self.assertEqual(20, result["stats"]["saved_new"])

    def test_data_contract_error_missing_path_ledgered(self):
        plan = site()
        plan["adapter"]["detail_args"] = ["html", {"source": "item", "path": ["optional"]}]
        board_rows = crawler(self.env, None)
        board_rows.listing_orig = board_rows.listing

        def listing(html):
            rows = board_rows.listing_orig(html)
            for row in rows:
                if int(row["external_id"]) % 5:
                    row["optional"] = row["external_id"]
            return rows
        board_rows.listing = listing
        board_rows.detail = lambda html, value: {"external_id": value, "url": "/view?id=" + value, "title": "t"}
        result = self.env.run(plan, FakeBoard(25, page_size=10), crawler=board_rows)
        self.assertEqual("complete_with_review", result["status"])
        _, failed = self.env.store.item_counts("fake-site")
        self.assertEqual(5, failed['failed_permanent:adapter_contract:missing_path:["optional"]'])

    def test_consecutive_data_contract_errors_stop(self):
        result = self.run_site(lambda html, item: {"url": item["url"], "title": "no identity"})
        self.assertEqual(("config_error", "adapter_contract_failures_consecutive:adapter_contract:paper_identity_missing"),
                         (result["status"], result["stop_reason"]))
        self.assertEqual(5, result["stats"]["failed_permanent"])

    def precheck(self, detail, rows):
        from tests.test_enumeration_review import reference_db
        ref = reference_db(rows)
        self.addCleanup(ref.close)
        return self.env.run(site(), FakeBoard(25, page_size=10), crawler=crawler(self.env, detail),
                            reference_conn=ref, identity_required=True)

    def test_identity_precheck_raises_contract_schema_error(self):
        def detail(html, item):
            url = item.get("detail_url") or item["url"]
            if url.endswith("=999"):
                return []
            return {"external_id": url.split("=")[1], "url": url, "title": "t"}
        result = self.precheck(detail, [("999", "https://board.test/view?id=999"),
                                        ("25", "https://board.test/view?id=25")])
        self.assertEqual(("config_error", "adapter_contract:paper_not_dict"), (result["status"], result["stop_reason"]))
        self.assertIsNone(result["identity"])
        self.assertEqual(0, result["stats"]["saved_new"])

    def test_identity_precheck_counts_listing_data_error_as_mismatch(self):
        def detail(html, item):
            if "detail_url" in item:  # reference row: list fields cannot be rebuilt from a URL
                return {"url": item["detail_url"], "title": "t"}
            if item["external_id"] == "24":
                return {"url": item["url"], "title": "t"}
            return {"external_id": item["external_id"], "url": "https://board.test" + item["url"], "title": "t"}
        result = self.precheck(detail, [(str(i), f"https://board.test/view?id={i}") for i in range(21, 26)])
        self.assertEqual("config_error", result["status"])
        self.assertTrue(result["stop_reason"].startswith("identity_mismatch"), result["stop_reason"])
        identity = result["identity"]
        self.assertEqual((5, 5, 4), (identity["errors"], identity["compared"], identity["matched"]))
        self.assertEqual(6, identity["contract_data_errors"])

    def test_json_source_with_non_json_listing_is_parse_error(self):
        from crawler.enumeration.http import Response
        from crawler.enumeration.parsing import ListingParser
        plan = site(adapter={"type": "crawler", "contract_version": 1, "list_source": "json", "paper_method": "paper"})
        plan["listing"]["parser"] = {"type": "adapter", "key_field": "id", "url_field": "link"}
        c = crawler(self.env, None)
        c.paper = lambda item: {"external_id": str(item["id"]), "url": item["link"], "title": "t"}
        from crawler.enumeration.adapters import Adapter
        page = ListingParser(plan["listing"]["parser"], adapter=Adapter("fake-site", plan["adapter"], None, crawler=c)) \
            .parse(Response(200, {"Content-Type": "text/html"}, "<html>maintenance</html>", "https://board.test/list"))
        self.assertEqual({"parse_error": "invalid_json"}, page.extra)
        result = self.env.run(plan, FakeBoard(5), crawler=c)
        self.assertEqual(("incomplete", "listing_failed:listing_parse_error:invalid_json"),
                         (result["status"], result["stop_reason"]))

    def test_failure_limits_validated(self):
        from crawler.enumeration.plan import PlanError
        for key, value in [("max_consecutive_contract_failures", 0), ("max_consecutive_contract_failures", True),
                           ("max_contract_failure_ratio", 1), ("max_contract_failure_ratio", "0.1")]:
            with self.subTest(key=key, value=value), self.assertRaisesRegex(PlanError, key):
                self.env.plan(site(**{key: value}))

    def test_non_contract_crawler_unchanged(self):
        # Without contract_version the legacy adapter path keeps its old outcome.
        plan = site(adapter={"type": "crawler", "list_method": "listing", "detail_method": "detail"})
        result = self.run_site(lambda html, item: None, plan)
        self.assertEqual("complete", result["status"])
        self.assertEqual(25, result["stats"]["failed_permanent"])


if __name__ == "__main__":
    unittest.main()
