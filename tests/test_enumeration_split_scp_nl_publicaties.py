"""scp-nl-publicaties: POST /api/search listing (ext:post_listing) + detail page.
The split methods must give crawl()'s exact Paper (identity included). No network."""
import ast
import contextlib
import copy
import hashlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import ROOT, load, split_fixture, split_plan

SITE = "scp-nl-publicaties"
SEARCH = "https://www.scp.nl/api/search"


class RecordingTransport:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, method, url, *, headers=None, data=None):
        self.calls.append((method, url, data))
        return self.handler(method, url, data)


class Base(unittest.TestCase):
    def setUp(self):
        self.fixture = split_fixture(SITE)
        self.plan = {"version": 1, "sites": {SITE: split_plan(SITE)}}
        self.denials = []

        def denied(*args, **kwargs):
            self.denials.append("attempted I/O")
            raise AssertionError("split test attempted network/process I/O")
        for name in ("socket.getaddrinfo", "socket.socket.connect", "socket.socket.connect_ex",
                     "subprocess.Popen._execute_child"):
            p = patch(name, denied)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: self.assertEqual([], self.denials))

    def native(self, fixture=None):
        """Unchanged crawl() with its curl helper replaced by the fixture."""
        fixture = fixture or self.fixture
        crawler, _ = load(SITE)
        captured, list_calls = [], []

        def curl(url, *, timeout=45, post_json=None):
            if url == SEARCH:
                list_calls.append(post_json)
                return fixture["listing_body"]
            return fixture["details"][url]
        crawler._curl = curl
        crawler.detail_delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch("time.sleep", lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertEqual(1, len(list_calls))
        return captured

    def pipeline(self, fixture=None):
        fixture = fixture or self.fixture
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section("adapter"), None, crawler=crawler)
        self.raw_papers = []
        original = crawler._enumeration_detail

        def capture(*args):
            result = original(*args)
            if result is not None:
                self.raw_papers.append(copy.deepcopy(result))
            return result
        crawler._enumeration_detail = capture
        resp = Response(200, {"Content-Type": "application/json"}, fixture["listing_body"], SEARCH)
        items = ListingParser(p.listing["parser"], adapter=adapter).parse(resp).items
        self.assertEqual(2, len(items))

        def fetch(url):
            self.assertIn(url, fixture["details"], "undeclared detail request")
            return Response(200, {"Content-Type": "text/html"}, fixture["details"][url], url)
        papers = []
        for item in items:
            paper = adapter.materialize(item, SimpleNamespace(get=fetch))
            if paper is not None:
                papers.append(paper)
        return papers


class ScpSplitTest(Base):
    def test_full_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(2, len(expected))
        actual = self.pipeline()
        self.assertEqual(expected, self.raw_papers)
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])
        # Stored post_number is the Elastic document id, as before.
        self.assertEqual(["doc-6acdfc939f0bb5835bb3b462", "doc-6ac2cc9f11495ca23ac8efb5"],
                         [paper_identity(p)[0] for p in actual])

    def test_short_abstract_skip_is_preserved(self):
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture["long_text"], "Kort."))
        self.assertEqual(1, len(self.native(f)))
        self.assertEqual(1, len(self.pipeline(f)))

    def test_rows_without_title_or_id_are_skipped_like_crawl(self):
        crawler, _ = load(SITE)
        data = json.loads(self.fixture["listing_body"])
        data["results"][0]["page_title"] = {"raw": ""}
        data["results"][1]["id"] = {"raw": ""}
        data["results"][1]["_meta"] = {}
        with guarded():
            self.assertEqual([], crawler._enumeration_items(data, SEARCH))

    def test_wrong_shape_is_a_listing_parse_error(self):
        crawler, _ = load(SITE)
        for bad in ({"ok": False, "message": "x"}, [], {"results": ["x"]}):
            with self.assertRaises(PermanentFailure) as ctx:
                with guarded():
                    crawler._enumeration_items(bad, SEARCH)
            self.assertTrue(ctx.exception.reason.startswith("listing_parse_error:"))

    def test_original_methods_are_unchanged(self):
        code = (ROOT / "crawler/sites/custom" / (SITE + ".py")).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fixture["original_method_source_sha256"].items():
            self.assertEqual(digest, actual[name], name)

    def test_plan_posts_crawl_query_with_one_large_page(self):
        lst = split_plan(SITE)["listing"]
        self.assertEqual(("POST", True, "json"), (lst["method"], lst["idempotent"], lst["body_type"]))
        state = lst["params"]["requestState"]
        crawler_mod = load(SITE)[1]
        self.assertEqual(crawler_mod._PUBLICATION_TYPES, state["filters"][0]["values"])
        self.assertEqual([{"field": "sort_date", "direction": "desc"}], state["sortList"])
        self.assertEqual(1000, state["resultsPerPage"])
        self.assertEqual(1000, lst["page_size"])
        self.assertEqual({"type": "json", "path": "totalResults"}, lst["total"])

    def test_shipped_plan_completes_end_to_end(self):
        """Real SiteRunner: robots, POST listing (the source ignores the top-level page key,
        so page 2 repeats page 1 = wrap end), detail pages, saving."""
        from tests.enumeration_fakes import Env
        fixture = self.fixture

        def handle(method, url, data):
            path = urlsplit(url).path
            if path == "/robots.txt":
                return Response(200, {"Content-Type": "text/plain"}, b"User-Agent: *\n", url)
            if url == SEARCH:
                self.assertEqual("POST", method)
                body = json.loads(data)
                self.assertEqual(1, body["requestState"]["current"])
                return Response(200, {"Content-Type": "application/json"}, fixture["listing_body"], url)
            if url in fixture["details"]:
                self.assertEqual("GET", method)
                return Response(200, {"Content-Type": "text/html"}, fixture["details"][url], url)
            return Response(404, {}, b"", url)
        site = dict(split_plan(SITE), request_interval_s=0)
        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, "site_id", None) == SITE)
            transport = RecordingTransport(handle)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(site, None, crawler=cls(env.conn, 0), transport=transport)
            docs = env.docs()
        finally:
            env.close()
        self.assertEqual("complete", result["status"], result)
        self.assertTrue(result["stop_reason"].startswith("last_page_"), result)
        self.assertEqual(["doc-6acdfc939f0bb5835bb3b462", "doc-6ac2cc9f11495ca23ac8efb5"],
                         sorted((d["post_number"] for d in docs), reverse=True))
        self.assertEqual("https://www.scp.nl/robots.txt", transport.calls[0][1])

    def test_nested_page_plan_pages_through_request_state(self):
        """ext:post_listing_nested_page (gx3): requestState.current = "{page}" with 1 row per page
        here; pages past the end answer an empty result list. Same Paper/identity as above."""
        from tests.enumeration_fakes import Env
        fixture = self.fixture
        rows = json.loads(fixture["listing_body"])
        site = copy.deepcopy(dict(split_plan(SITE), request_interval_s=0))
        body = site["listing"].pop("params")
        body["requestState"]["current"] = "{page}"
        body["requestState"]["resultsPerPage"] = 1
        site["listing"]["body"] = body
        site["listing"]["page_size"] = 1
        site["listing"].pop("page_param", None)
        site_plan({"version": 1, "sites": {SITE: dict(site, request_interval_s=1.5)}}, SITE, registered=[SITE])
        currents = []

        def handle(method, url, data):
            path = urlsplit(url).path
            if path == "/robots.txt":
                return Response(200, {"Content-Type": "text/plain"}, b"User-Agent: *\n", url)
            if url == SEARCH:
                req = json.loads(data)
                cur = req["requestState"]["current"]
                self.assertIsInstance(cur, int)
                self.assertNotIn("page", req)
                currents.append(cur)
                page = dict(rows, results=rows["results"][cur - 1:cur])
                return Response(200, {"Content-Type": "application/json"}, json.dumps(page), url)
            if url in fixture["details"]:
                return Response(200, {"Content-Type": "text/html"}, fixture["details"][url], url)
            return Response(404, {}, b"", url)
        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, "site_id", None) == SITE)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(site, None, crawler=cls(env.conn, 0), transport=RecordingTransport(handle))
            docs = env.docs()
        finally:
            env.close()
        self.assertEqual("complete", result["status"], result)
        self.assertEqual(["doc-6acdfc939f0bb5835bb3b462", "doc-6ac2cc9f11495ca23ac8efb5"],
                         sorted((d["post_number"] for d in docs), reverse=True))
        self.assertIn(2, currents)
        self.assertIn(3, currents)  # past the end: empty result list


if __name__ == "__main__":
    unittest.main()
