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
