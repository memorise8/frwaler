"""nkvts-no-english: Algolia POST listing (ext:post_listing + ext:headers, key from the
environment only) + detail page. The split methods must give crawl()'s exact Paper. No network."""
import ast
import contextlib
import copy
import hashlib
import io
import json
import json as _json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import PlanError, header_hosts, resolve_headers, site_plan
from tests.collector_split_helpers import ROOT, load, split_fixture, split_plan

SITE = "nkvts-no-english"
SEARCH = "https://a4eox51zo9-dsn.algolia.net/1/indexes/wp_searchable_posts/query"


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
        """Unchanged crawl() with its HTTP session replaced by the fixture."""
        fixture = fixture or self.fixture
        crawler, _ = load(SITE)
        captured, list_calls = [], []

        def post(url, json=None, headers=None, timeout=None):
            list_calls.append(json)
            body = fixture["listing_body"] if json["page"] == 0 else '{"hits": [], "nbPages": 1}'
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: _json.loads(body))

        def get(url, timeout=None):
            return SimpleNamespace(raise_for_status=lambda: None, text=fixture["details"][url])
        crawler._session = SimpleNamespace(post=post, get=get)
        crawler._delay = 0
        crawler._source_failure = None
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch("time.sleep", lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertLessEqual(len(list_calls), 2)
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


class NkvtsSplitTest(Base):
    def test_full_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(2, len(expected))
        actual = self.pipeline()
        self.assertEqual(expected, self.raw_papers)
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])
        self.assertEqual(["8788", "8789"], [paper_identity(p)[0] for p in actual])

    def test_short_abstract_skip_is_preserved(self):
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture["long_text"], "Kort."))
        self.assertEqual(1, len(self.native(f)))
        self.assertEqual(1, len(self.pipeline(f)))

    def test_split_records_and_missing_permalinks_are_skipped_like_crawl(self):
        crawler, _ = load(SITE)
        data = json.loads(self.fixture["listing_body"])
        with guarded():
            rows = crawler._enumeration_items(data, SEARCH)
        self.assertEqual([8788, 8789], [r["post_id"] for r in rows])  # 8788-1 is a split record
        self.assertEqual("2025-01-01", rows[1]["published_date"])  # year-only fallback of crawl()
        data["hits"][0]["permalink"] = ""
        data["hits"][2]["post_id"] = None
        with guarded():
            self.assertEqual([], crawler._enumeration_items(data, SEARCH))

    def test_wrong_shape_is_a_listing_parse_error(self):
        crawler, _ = load(SITE)
        for bad in ({"message": "Invalid Application-ID or API key", "status": 403}, [], {"hits": ["x"]}):
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

    def test_key_comes_from_the_environment_and_goes_to_algolia_only(self):
        site = split_plan(SITE)
        self.assertNotIn("X-Algolia-API-Key", site.get("request_headers", {}))
        self.assertEqual(["a4eox51zo9-dsn.algolia.net"], header_hosts(site))
        with self.assertRaises(PlanError):
            resolve_headers(site, environ={})
        headers = resolve_headers(site, environ={"NKVTS_NO_ENGLISH_KEY": "test-value"})
        self.assertEqual({"X-Algolia-Application-Id": "A4EOX51ZO9", "X-Algolia-API-Key": "test-value"}, headers)
        lst = site["listing"]
        self.assertEqual({"query": "", "hitsPerPage": 50, "facetFilters": ["post_type_label:Academic Articles"]},
                         lst["params"])
        self.assertEqual(("page", 0, 50), (lst["page_param"], lst["start_page"], lst["page_size"]))

    def test_shipped_plan_completes_end_to_end(self):
        """Real SiteRunner: robots, POST listing pages 0.. (empty hits past the end), details."""
        from tests.enumeration_fakes import Env
        fixture = self.fixture
        pages = []

        def handle(method, url, data):
            path = urlsplit(url).path
            if path == "/robots.txt":
                return Response(404, {}, b"", url)
            if url == SEARCH:
                self.assertEqual("POST", method)
                body = json.loads(data)
                pages.append(body.get("page", 0))  # Algolia default page is 0 (page-less start check)
                text = fixture["listing_body"] if body.get("page", 0) == 0 else json.dumps({"hits": [], "nbPages": 1})
                return Response(200, {"Content-Type": "application/json"}, text, url)
            if url in fixture["details"]:
                return Response(200, {"Content-Type": "text/html"}, fixture["details"][url], url)
            return Response(404, {}, b"", url)
        site = dict(split_plan(SITE), request_interval_s=0)
        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, "site_id", None) == SITE)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(site, None, crawler=cls(env.conn), transport=RecordingTransport(handle))
            docs = env.docs()
        finally:
            env.close()
        self.assertEqual("complete", result["status"], result)
        self.assertTrue(result["stop_reason"].startswith("last_page_"), result)
        self.assertEqual(["8788", "8789"], sorted(d["post_number"] for d in docs))
        self.assertIn(0, pages)


if __name__ == "__main__":
    unittest.main()
