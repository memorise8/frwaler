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


class IdentityMissingReview(Base):
    """MEDIUM-2: missing identity/URL is never tolerated by max_contract_failure_ratio."""

    @staticmethod
    def detail(missing=("7", "13"), broken=()):
        def detail(html, item):
            if item["external_id"] in missing:
                return {"url": item["url"], "title": "no identity"}
            if item["external_id"] in broken:
                raise KeyError("slug")
            return good(html, item)
        return detail

    def test_ratio_does_not_hide_identity_missing(self):
        result = self.run_site(self.detail(), site(max_contract_failure_ratio=0.5))
        self.assertEqual(("complete_with_review", "identity_missing:2"), (result["status"], result["stop_reason"]))

    def test_url_missing_counts_as_identity_missing(self):
        def detail(html, item):
            if item["external_id"] == "9":
                return {"external_id": "9", "title": "no url"}
            return good(html, item)
        plan = site(max_contract_failure_ratio=0.5)
        board = crawler(self.env, detail)
        result = self.env.run(plan, self.board, crawler=board)
        self.assertEqual(("complete_with_review", "identity_missing:1"), (result["status"], result["stop_reason"]))

    def test_ratio_tolerates_parse_errors_but_not_identity(self):
        result = self.run_site(self.detail(missing=("7",), broken=("13",)), site(max_contract_failure_ratio=0.5))
        self.assertEqual(("complete_with_review", "identity_missing:1"), (result["status"], result["stop_reason"]))

    def test_default_ratio_reports_both(self):
        result = self.run_site(self.detail(missing=("7",), broken=("13",)))
        self.assertEqual("complete_with_review", result["status"])
        self.assertEqual({"identity_missing:1", "adapter_contract_failures:1"}, set(result["stop_reason"].split(";")))


class QueryBindingAfterRedirect(Base):
    """LOW-3: detail/paper query bindings read the item URL as requested."""

    def test_query_binding_uses_item_url(self):
        plan = site()
        plan["adapter"]["detail_args"] = ["html", {"source": "query", "path": ["id"]}]
        c = crawler(self.env, None)
        c.detail = lambda html, ident: {"external_id": ident, "url": f"https://board.test/view?id={ident}",
                                        "title": "t"}
        result = self.env.run(plan, Redirecting(3, page_size=10), crawler=c)
        self.assertEqual("complete", result["status"], result)
        self.assertEqual(["1", "2", "3"], sorted(d["post_number"] for d in self.env.docs()))

    def test_final_url_still_available(self):
        plan = site()
        plan["adapter"]["detail_args"] = ["html", "final_url", "item"]
        c = crawler(self.env, None)
        c.detail = lambda html, url, item: {"external_id": item["external_id"], "url": url, "title": "t"}
        result = self.env.run(plan, Redirecting(3, page_size=10), crawler=c)
        self.assertEqual("complete", result["status"], result)
        self.assertTrue(all("/article/" in d["meta_url"] for d in self.env.docs()))


class PrecheckNonContract(Base):
    """LOW-6: a non-contract site's AdapterConfigError during the identity sample is a
    sample error, exactly as before the contract was added."""

    def test_non_contract_config_error_counts_as_sample_error(self):
        from tests.test_enumeration_review import reference_db
        ref = reference_db([(str(i), f"https://board.test/view?id={i}") for i in range(21, 26)])
        self.addCleanup(ref.close)

        def detail(html, item):
            if "external_id" not in item:  # reference sample: URL only
                raise AdapterConfigError("crawler_side_value_error")
            return {"external_id": item["external_id"], "url": "https://board.test" + item["url"], "title": "t"}
        plan = site(adapter={"type": "crawler", "list_method": "listing", "detail_method": "detail"})
        result = self.env.run(plan, self.board, crawler=crawler(self.env, detail), reference_conn=ref,
                              identity_required=True)
        self.assertEqual("complete", result["status"], result)
        self.assertEqual((5, 5, 5), (result["identity"]["errors"], result["identity"]["compared"],
                                     result["identity"]["matched"]))
        self.assertNotIn("contract_data_errors", result["identity"])

    def test_contract_schema_error_still_raised(self):
        from tests.test_enumeration_review import reference_db
        ref = reference_db([("25", "https://board.test/view?id=25")])
        self.addCleanup(ref.close)
        result = self.env.run(site(), self.board, crawler=crawler(self.env, lambda html, item: []),
                              reference_conn=ref, identity_required=True)
        self.assertEqual(("config_error", "adapter_contract:paper_not_dict"), (result["status"], result["stop_reason"]))


class JsonBoundListMethod(unittest.TestCase):
    """A contract list method fed the "json" token reads a JSON listing: an empty list
    is an explicit empty page and a non-JSON body a listing parse error, exactly as
    with list_source "json" (needed by JSON APIs such as NTRS)."""

    def parse(self, list_args, body, content_type="application/json"):
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.parsing import ListingParser
        env = Env()
        self.addCleanup(env.close)
        c = crawler(env, good)
        c.rows = lambda data: [{"url": f"/view?id={r['id']}", "external_id": str(r["id"]), "title": "t"}
                               for r in data["results"]]
        cfg = {"type": "crawler", "contract_version": 1, "list_method": "rows", "list_args": list_args,
               "detail_method": "detail"}
        parser = ListingParser({"type": "adapter"}, adapter=Adapter(SITE, cfg, None, crawler=c))
        return parser.parse(Response(200, {"Content-Type": content_type}, body, "https://board.test/api"))

    def test_empty_json_list_is_explicit_empty(self):
        for args in (["json"], [{"source": "json", "path": []}]):
            page = self.parse(args, '{"results": []}')
            self.assertEqual({"explicit_empty": True}, page.extra, args)

    def test_non_json_body_is_invalid_json(self):
        page = self.parse(["json"], "<html>maintenance</html>", "text/html")
        self.assertEqual({"parse_error": "invalid_json"}, page.extra)

    def test_html_bound_method_still_needs_marker(self):
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.parsing import ListingParser
        env = Env()
        self.addCleanup(env.close)
        c = crawler(env, good)
        cfg = {"type": "crawler", "contract_version": 1, "list_method": "listing", "detail_method": "detail"}
        page = ListingParser({"type": "adapter"}, adapter=Adapter(SITE, cfg, None, crawler=c)).parse(
            Response(200, {"Content-Type": "text/html"}, "<html><p>nothing</p></html>", "https://board.test/list"))
        self.assertEqual({"parse_error": "empty_without_listing_marker"}, page.extra)


if __name__ == "__main__":
    unittest.main()
