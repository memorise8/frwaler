"""Independent re-review (COMMENT) findings on the adapter contract: MEDIUM-1/2, LOW-3/6
and the missing tests. Fake boards and in-memory transports only (no network)."""
import unittest
from urllib.parse import parse_qs, urlsplit

from crawler.enumeration.adapters import AdapterConfigError
from crawler.enumeration.http import Response
from tests.enumeration_fakes import Env, FakeBoard
from tests.test_enumeration_adapter_runner import crawler, good, site

SITE = "fake-site"


def none_detail(html, item):
    return None


class Redirecting(FakeBoard):
    """Detail links /view?id=N answer 301 to /article/N (no query string)."""

    def __call__(self, url):
        parts = urlsplit(url)
        if parts.path == "/view":
            return Response(301, {"Location": f"/article/{parse_qs(parts.query)['id'][0]}"}, b"")
        if parts.path.startswith("/article/"):
            return self.detail(int(parts.path.rsplit("/", 1)[1]))
        return super().__call__(url)


class Base(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.board = FakeBoard(25, page_size=10)

    def run_site(self, detail, plan=None, **kw):
        return self.env.run(plan or site(), self.board, crawler=crawler(self.env, detail), **kw)

    def failed(self):
        _, failed = self.env.store.item_counts(SITE)
        return failed

    def ledger(self, status):
        return self.env.store.conn.execute("SELECT COUNT(*) FROM enum_item WHERE site_id=? AND status=?",
                                           (SITE, status)).fetchone()[0]

    def listed(self):
        return self.env.store.conn.execute("SELECT COUNT(*) FROM enum_listed WHERE site_id=?", (SITE,)).fetchone()[0]


class ConsecutiveStopResume(Base):
    """MEDIUM-1: a plain rerun after a consecutive contract stop must not skip the
    failed items as known and advance the checkpoint past them."""

    def test_plain_rerun_is_refused_without_requests(self):
        first = self.run_site(none_detail)
        self.assertEqual(("config_error", "adapter_contract_failures_consecutive:empty_item"),
                         (first["status"], first["stop_reason"]))
        again = self.run_site(good)
        self.assertEqual(("config_error", "adapter_contract_stop_requires_reprocess"),
                         (again["status"], again["stop_reason"]))
        self.assertEqual([], again["transport"].urls)
        self.assertEqual((5, 0, 0), (self.ledger("failed_permanent"), self.ledger("saved"), self.listed()))
        self.assertEqual("adapter_contract_failures_consecutive:empty_item",
                         self.env.store.site(SITE)["stop_reason"])

    def test_repeated_reruns_never_freeze_more_items(self):
        for _ in range(8):
            self.run_site(none_detail)
        self.assertEqual((5, 0), (self.ledger("failed_permanent"), self.listed()))

    def test_reprocess_failed_releases_and_retries_the_items(self):
        self.run_site(none_detail)
        self.assertEqual(5, self.env.store.requeue_failed(SITE, ["empty_item"]))
        result = self.run_site(good)
        self.assertEqual("complete", result["status"], result)
        self.assertEqual(25, result["stats"]["saved_new"])
        self.assertEqual(5, result["stats"]["retried"])
        self.assertEqual({}, self.failed())

    def test_requeued_items_still_broken_stop_again(self):
        self.run_site(none_detail)
        self.env.store.requeue_failed(SITE, ["empty_item"])
        result = self.run_site(none_detail)
        self.assertEqual(("config_error", "adapter_contract_failures_consecutive:empty_item"),
                         (result["status"], result["stop_reason"]))
        again = self.run_site(good)
        self.assertEqual("adapter_contract_stop_requires_reprocess", again["stop_reason"])
        self.assertEqual((5, 0), (self.ledger("failed_permanent"), self.listed()))

    def test_retry_only_is_refused_too(self):
        self.run_site(none_detail)
        result = self.run_site(good, retry_only=True)
        self.assertEqual(("config_error", "adapter_contract_stop_requires_reprocess"),
                         (result["status"], result["stop_reason"]))

    def test_refresh_resumes_but_never_plain_complete(self):
        self.run_site(none_detail)
        result = self.run_site(good, refresh=True)
        self.assertEqual("complete_with_review", result["status"])
        self.assertIn("adapter_contract_failures:5", result["stop_reason"])
        self.assertEqual(20, result["stats"]["saved_new"])

    def test_plan_change_keeps_the_hold(self):
        self.run_site(none_detail)
        plan = site()
        plan["listing"]["end_statuses"] = [404]  # a different plan fingerprint
        result = self.run_site(good, plan)
        self.assertEqual("adapter_contract_stop_requires_reprocess", result["stop_reason"])

    def test_stop_records_the_streak_items(self):
        self.run_site(none_detail)
        keys = self.env.store.site(SITE)["state"]["adapter_contract_stop_items"]
        self.assertEqual(["1", "2", "3", "4", "5"], keys)  # oldest page first, oldest item first

    def test_permanent_failure_does_not_reset_the_counter(self):
        self.board.detail_status = {3: 404}
        result = self.run_site(none_detail)
        self.assertEqual(("config_error", "adapter_contract_failures_consecutive:empty_item"),
                         (result["status"], result["stop_reason"]))
        # 1, 2 (None), 3 (404: not a contract failure, no reset), 4, 5, 6 (None) -> stop.
        self.assertEqual(6, result["stats"]["failed_permanent"])
        self.assertEqual(["1", "2", "4", "5", "6"],
                         self.env.store.site(SITE)["state"]["adapter_contract_stop_items"])

    def test_non_contract_site_unchanged(self):
        plan = site(adapter={"type": "crawler", "list_method": "listing", "detail_method": "detail"})
        self.assertEqual("complete", self.run_site(none_detail, plan)["status"])
        self.assertEqual("already_complete_retry_only", self.run_site(good, plan)["stop_reason"])



if __name__ == "__main__":
    unittest.main()
