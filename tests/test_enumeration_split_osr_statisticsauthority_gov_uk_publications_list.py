"""osr-statisticsauthority-gov-uk-publications-list: WP REST listing + detail page (first step)
+ media API (second step from url_template, ext:detail_aux_request + ext:two_step_keep_first).
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
from urllib.parse import parse_qs, urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import ROOT, load, split_fixture, split_plan, wp_past_last_page

SITE = "osr-statisticsauthority-gov-uk-publications-list"
API = "https://osr.statisticsauthority.gov.uk/wp-json/wp/v2"


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
        captured = []

        def curl(url, retries=3):
            if url.startswith(API + "/publication-type"):
                return b"[]"
            if url.startswith(API + "/publication?"):
                return (fixture["listing_body"] if "page=1&" in url else "[]").encode()
            if url in fixture["media"]:
                return fixture["media"][url].encode()
            return fixture["details"][url].encode()
        crawler._curl_get = curl
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch("time.sleep", lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        return captured

    def pipeline(self, fixture=None):
        fixture = fixture or self.fixture
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section("adapter"), None, crawler=crawler)
        self.raw_papers, self.requested = [], []
        original = crawler._enumeration_detail

        def capture(*args):
            result = original(*args)
            if result is not None:
                self.raw_papers.append(copy.deepcopy(result))
            return result
        crawler._enumeration_detail = capture
        url = API + "/publication?per_page=100&orderby=date&order=desc&_embed=1&page=1"
        resp = Response(200, {"Content-Type": "application/json"}, fixture["listing_body"], url)
        items = ListingParser(p.listing["parser"], adapter=adapter).parse(resp).items
        self.assertEqual(2, len(items))

        def fetch(url):
            self.requested.append(url)
            if url in fixture["media"]:
                return Response(200, {"Content-Type": "application/json"}, fixture["media"][url], url)
            self.assertIn(url, fixture["details"], "undeclared detail request")
            return Response(200, {"Content-Type": "text/html; charset=UTF-8"}, fixture["details"][url], url)
        papers = []
        for item in items:
            paper = adapter.materialize(item, SimpleNamespace(get=fetch))
            if paper is not None:
                papers.append(paper)
        return papers


class OsrSplitTest(Base):
    def test_full_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(2, len(expected))
        actual = self.pipeline()
        self.assertEqual(expected, self.raw_papers)
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])
        self.assertEqual(["57828", "57707"], [paper_identity(p)[0] for p in actual])
        # pdf_url / original_filename come from the media API (second step), as in crawl().
        self.assertTrue(all(p["pdf_url"].endswith(".pdf") for p in actual))
        self.assertTrue(all(p["original_filename"].endswith(".pdf") for p in actual))
        # detail page first, then the media request built from the WP id
        self.assertEqual([list(self.fixture["details"])[0]], self.requested[:1])
        self.assertIn(API + "/media?parent=57828&mime_type=application/pdf", self.requested)

    def test_empty_media_gives_no_pdf_like_crawl(self):
        f = copy.deepcopy(self.fixture)
        f["media"] = {k: "[]" for k in f["media"]}
        expected = self.native(f)
        actual = self.pipeline(f)
        self.assertEqual(expected, self.raw_papers)
        self.assertEqual([None, None], [p.get("pdf_url") for p in expected])
        self.assertEqual([clean_paper(p) for p in expected], actual)

    def test_short_abstract_skip_is_preserved(self):
        f = copy.deepcopy(self.fixture)
        first = list(f["details"])[0]
        f["details"][first] = "<html><body><main><p>Published: 1 May 2026</p></main></body></html>"
        self.assertEqual(1, len(self.native(f)))
        self.assertEqual(1, len(self.pipeline(f)))

    def test_publication_type_ids_are_refused_not_stored_differently(self):
        crawler, _ = load(SITE)
        row = crawler._enumeration_items(json.loads(self.fixture["listing_body"]))[0]
        row["item"]["publication-type"] = [12]
        with self.assertRaises(PermanentFailure) as ctx:
            with guarded():
                crawler._enumeration_detail(b"[]", SimpleNamespace(body=b"<main><p>x</p></main>"), row)
        self.assertEqual("publication_type_map_needed", ctx.exception.reason)

    def test_wrong_shape_is_a_listing_parse_error(self):
        crawler, _ = load(SITE)
        for bad in ({"code": "x"}, ["x"]):
            with self.assertRaises(PermanentFailure) as ctx:
                with guarded():
                    crawler._enumeration_items(bad, API)
            self.assertTrue(ctx.exception.reason.startswith("listing_parse_error:"))

    def test_original_methods_are_unchanged(self):
        code = (ROOT / "crawler/sites/custom" / (SITE + ".py")).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fixture["original_method_source_sha256"].items():
            self.assertEqual(digest, actual[name], name)

    def test_shipped_plan_completes_end_to_end(self):
        """Real SiteRunner: robots, WP REST pages (400 past the end), detail page then media API."""
        from tests.enumeration_fakes import Env
        fixture = self.fixture

        def handle(method, url, data):
            parts = urlsplit(url)
            if parts.path == "/robots.txt":
                return Response(200, {"Content-Type": "text/plain"}, b"User-Agent: *\n", url)
            if parts.path == "/wp-json/wp/v2/publication":
                if parse_qs(parts.query).get("page", ["1"]) == ["1"]:
                    return Response(200, {"Content-Type": "application/json", "X-WP-Total": "2"},
                                    fixture["listing_body"], url)
                return wp_past_last_page(url)
            if url in fixture["media"]:
                return Response(200, {"Content-Type": "application/json"}, fixture["media"][url], url)
            if url in fixture["details"]:
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
        self.assertEqual(["57707", "57828"], sorted(d["post_number"] for d in docs))
        media = [c[1] for c in transport.calls if "/wp/v2/media" in c[1]]
        self.assertEqual(2, len(media))


if __name__ == "__main__":
    unittest.main()
