"""ext:jsonp: JSON listings wrapped in one callback call (llnl-gov-news style). No network."""
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from crawler.base_crawler import BaseCrawler
from crawler.enumeration.http import Response
from crawler.enumeration.plan import PlanError
from tests.enumeration_fakes import Env, FakeBoard
from tests.test_enumeration_extensions import API_PARSER, RecordingTransport, api_items, api_plan


def jsonp(data, prefix="/**/eventsLoaded("):
    return Response(200, {"Content-Type": "application/javascript"}, prefix + json.dumps(data) + ");")


def paged(ids, page_size=3, wrap=lambda data: {"items": data}):
    """Oldest-first JSONP API: ?page=N, an empty list after the last page."""
    def handle(method, url, data):
        parts = urlsplit(url)
        if parts.path == "/robots.txt":
            return Response(404, {}, b"")
        page = int(parse_qs(parts.query).get("page", ["1"])[0])
        return jsonp(wrap(api_items(ids[(page - 1) * page_size:page * page_size])))
    return handle


class ResponseJsonpTest(unittest.TestCase):
    def test_reads_callback_argument(self):
        self.assertEqual([{"a": 1}], jsonp([{"a": 1}]).json(jsonp=True))
        self.assertEqual({"x": [1]}, Response(200, {}, ' cb.ns_1({"x": [1]})\n').json(jsonp=True))
        self.assertEqual([], Response(200, {}, "eventsLoaded([])").json(jsonp=True))
        self.assertEqual([{"t": "a) b(c"}], Response(200, {}, 'cb([{"t": "a) b(c"}]);').json(jsonp=True))

    def test_other_bodies_are_errors(self):
        for body in ('[{"a": 1}]', "eventsLoaded([1]); alert(1)", "<html>error</html>", "a(b)(c)", ""):
            with self.assertRaises(ValueError, msg=body):
                Response(200, {}, body).json(jsonp=True)
        with self.assertRaises(ValueError):
            jsonp([1]).json()  # without the option a JSONP body stays invalid JSON


class JsonpListingTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def site(self, **parser):
        return api_plan(listing={"parser": {**API_PARSER, "jsonp": True, **parser}})

    def test_json_parser_completes_on_empty_callback_list(self):
        transport = RecordingTransport(paged(list(range(1, 8))))
        result = self.env.run(self.site(), None, transport=transport)
        self.assertEqual("complete", result["status"], result)
        self.assertEqual(list(range(1, 8)), sorted(int(d["post_number"]) for d in self.env.docs()))

    def test_without_option_jsonp_is_a_parse_error_not_an_end(self):
        result = self.env.run(api_plan(), None, transport=RecordingTransport(paged([1, 2])))
        self.assertNotEqual("complete", result["status"])
        self.assertEqual([], self.env.docs())

    def test_validation(self):
        with self.assertRaises(PlanError):
            self.env.plan(self.site(jsonp="yes"))
        with self.assertRaises(PlanError):
            self.env.plan(api_plan(listing={"parser": {"type": "html", "item": "a", "jsonp": True}}))
        with self.assertRaises(PlanError):
            self.env.plan(api_plan(adapter={"type": "list_only", "jsonp": 1}))
        with self.assertRaises(PlanError):  # cursors read plain JSON
            self.env.plan(api_plan(listing={"parser": {**API_PARSER, "jsonp": True},
                                            "next_cursor": {"path": "next", "param": "after"}}))
        self.env.plan(self.site(jsonp=False))


class JsonpAdapterTest(unittest.TestCase):
    """A registered crawler's list method bound to the json token (contract adapter)."""

    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def crawler(self):
        class Api(BaseCrawler):
            site_id = "fake-site"
            site_name = "fake-site"
            base_url = "https://board.test"

            def crawl(self, limit=None):
                pass

            def listing(self, data):
                return [{"url": f"https://board.test/view?id={a['id']}", "external_id": str(a["id"]),
                         "title": a["title"]} for a in data]

            def detail(self, html, item):
                return {"external_id": item["external_id"], "url": item["url"], "title": item["title"]}
        return Api(self.env.conn, 0)

    def site(self, **adapter):
        return {"strategy": "oldest_first_sort", "request_interval_s": 0, "base_url": "https://board.test",
                "listing": {"url": "https://board.test/api", "page_param": "page", "start_page": 1,
                            "parser": {"type": "adapter"}},
                "adapter": {"type": "crawler", "contract_version": 1, "list_method": "listing", "list_args": ["json"],
                            "detail_method": "detail", **adapter}}

    def handler(self, n):
        board, api = FakeBoard(n), paged(list(range(1, n + 1)), wrap=lambda data: data)

        def handle(method, url, data):
            return api(method, url, data) if urlsplit(url).path in ("/api", "/robots.txt") else board(url)
        return handle

    def test_adapter_json_token_reads_jsonp_and_ends_on_empty_list(self):
        result = self.env.run(self.site(jsonp=True), None, transport=RecordingTransport(self.handler(7)),
                              crawler=self.crawler())
        self.assertEqual("complete", result["status"], result)
        self.assertEqual(list(range(1, 8)), sorted(int(d["post_number"]) for d in self.env.docs()))

    def test_adapter_without_option_stops(self):
        result = self.env.run(self.site(), None, transport=RecordingTransport(self.handler(4)),
                              crawler=self.crawler())
        self.assertNotEqual("complete", result["status"])
        self.assertEqual([], self.env.docs())


if __name__ == "__main__":
    unittest.main()
