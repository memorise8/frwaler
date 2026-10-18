"""ameslab-gov-news: three listings as explicit segments with their own URLs (ext:multi_listing),
detail page per item. The split methods must give crawl()'s exact Paper (identity included);
the HEAD request crawl() sends for a PDF link without a file name is never made. No network."""
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
from tests.collector_split_helpers import ROOT, load, split_fixture, split_plan

SITE = "ameslab-gov-news"
BASE = "https://www.ameslab.gov"
COOLER = BASE + "/news/cooler-10"
RARE = BASE + "/news/rare-earth-magnet-recycling-wins-flc-innovation-award"


class RecordingTransport:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, method, url, *, headers=None, data=None):
        self.calls.append((method, url, data))
        return self.handler(method, url, data)


def details(fixture, pdf_href="/sites/default/files/2026-01/ames-report.pdf"):
    """The fetched detail page, and a second one under RARE with another node id and a PDF link."""
    other = fixture["detail"].replace('node\\/576', 'node\\/1999').replace(
        '<div class="textual md:col-constrain-3">',
        f'<div class="textual md:col-constrain-3"><p><a href="{pdf_href}">Report (PDF)</a></p>', 1)
    assert other != fixture["detail"]
    return {COOLER: fixture["detail"], RARE: other}


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

    def native(self, pages, detail_pages, head=None):
        """Unchanged crawl() with curl replaced: ``pages`` maps listing URLs to HTML."""
        crawler, _ = load(SITE)
        crawler._delay = 0
        captured, heads = [], []

        def curl(url, *, label="", timeout=45):
            if url in pages:
                return pages[url]
            if url.startswith(BASE + "/news/") and "?" not in url and url.rsplit("/", 1)[-1] in (
                    "news-releases", "in-the-news", "feature-stories") or "?page=" in url:
                return self.fixture["empty"]
            return detail_pages[url]
        crawler._curl_get = curl

        def head_cd(url):
            heads.append(url)
            return head
        crawler._curl_head_content_disposition = head_cd
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._report_source_failure = lambda *a: None
        with patch("time.sleep", lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.heads = heads
        return captured

    def pipeline(self, list_url, detail_pages):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section("adapter"), None, crawler=crawler)
        resp = Response(200, {"Content-Type": "text/html; charset=UTF-8"}, self.fixture["listing"], list_url)
        items = ListingParser(p.listing["parser"], adapter=adapter).parse(resp).items
        self.assertEqual(2, len(items))
        out = []
        for item in items:
            def fetch(url):
                self.assertIn(url, detail_pages, "undeclared detail request")
                return Response(200, {"Content-Type": "text/html; charset=UTF-8"}, detail_pages[url], url)
            out.append(adapter.materialize(item, SimpleNamespace(get=fetch)))
        return out


class AmeslabSplitTest(Base):
    def test_full_paper_and_identity_equal_original_crawl(self):
        pages = {BASE + "/news/feature-stories": self.fixture["listing"]}
        dp = details(self.fixture)
        expected = self.native(pages, dp)
        self.assertEqual(2, len(expected))
        self.assertEqual([], self.heads)  # the PDF link has a file name: no HEAD in crawl() either
        actual = self.pipeline(BASE + "/news/feature-stories", dp)
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])
        self.assertEqual(["1999", "576"], [paper_identity(p)[0] for p in actual])
        self.assertEqual(["Feature Story"] * 2, [p["category"] for p in actual])
        self.assertTrue(actual[0]["pdf_url"].endswith("/ames-report.pdf"))
        self.assertEqual("ames-report.pdf", actual[0]["original_filename"])

    def test_category_and_list_endpoint_follow_the_listing_url(self):
        url = BASE + "/news/in-the-news?page=3"
        pages = {BASE + "/news/in-the-news?page=3": self.fixture["listing"]}
        dp = details(self.fixture)
        crawler, _ = load(SITE)
        with guarded():
            rows = crawler._enumeration_items(self.fixture["listing"], url)
        self.assertEqual({"In the News"}, {r["category"] for r in rows})
        self.assertEqual({url}, {r["list_endpoint"] for r in rows})
        # crawl() reads in-the-news page 3 only after pages 0-2; compare the Paper directly.
        native, _ = load(SITE)
        native._curl_head_content_disposition = lambda u: None
        expected = [native._build_paper(r, native._parse_detail_page(dp[r["url"]], r["url"], "In the News"))
                    for r in native._parse_list_page(pages[url], url, "In the News")]
        actual = self.pipeline(url, dp)
        self.assertEqual([clean_paper(p) for p in expected], actual)
        with self.assertRaises(PermanentFailure):
            with guarded():
                crawler._enumeration_items(self.fixture["listing"], BASE + "/news/other")

    def test_pdf_link_without_file_name_fails_instead_of_a_head_request(self):
        pages = {BASE + "/news/feature-stories": self.fixture["listing"]}
        dp = details(self.fixture, pdf_href="/media/download?file=report.pdf")
        expected = self.native(pages, dp, head='attachment; filename="Ames Report.pdf"')
        self.assertEqual([BASE + "/media/download?file=report.pdf"], self.heads)
        self.assertEqual("Ames Report.pdf", expected[0]["original_filename"])
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section("adapter"), None, crawler=crawler)
        resp = Response(200, {"Content-Type": "text/html"}, self.fixture["listing"], BASE + "/news/feature-stories")
        items = ListingParser(p.listing["parser"], adapter=adapter).parse(resp).items
        fetch = SimpleNamespace(get=lambda url: Response(200, {"Content-Type": "text/html"}, dp[url], url))
        self.assertEqual(clean_paper(expected[1]), adapter.materialize(items[1], fetch))
        with self.assertRaises(PermanentFailure) as ctx:
            adapter.materialize(items[0], fetch)
        self.assertEqual("original_filename_needs_head_request", ctx.exception.reason)

    def test_short_abstract_skip_is_preserved(self):
        crawler, _ = load(SITE)
        row = {"title": "T", "url": COOLER, "list_snippet": "", "category": "Feature Story", "list_endpoint": "x"}
        short = '<html><body><main><section class="hero"><h1>T</h1></section><div class="textual"><p>Short.</p></div></main></body></html>'
        with guarded():
            self.assertIsNone(crawler._enumeration_detail(short, COOLER, row))

    def test_original_methods_are_unchanged(self):
        code = (ROOT / "crawler/sites/custom" / (SITE + ".py")).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fixture["original_method_source_sha256"].items():
            self.assertEqual(digest, actual[name], name)

    def test_shipped_plan_reads_three_listings_end_to_end(self):
        """Real SiteRunner: explicit segments with their own URLs, bare first page, ?page=N after,
        empty page (listing marker) ends each listing; an article in two listings is saved once."""
        from tests.enumeration_fakes import Env
        fixture = self.fixture
        dp = details(fixture)

        def handle(method, url, data):
            parts = urlsplit(url)
            if parts.path == "/robots.txt":
                return Response(200, {"Content-Type": "text/plain"}, b"User-Agent: *\n", url)
            if url in dp:
                return Response(200, {"Content-Type": "text/html"}, dp[url], url)
            slug = parts.path.rsplit("/", 1)[-1]
            page = int(parse_qs(parts.query).get("page", ["0"])[0])
            if slug in ("feature-stories", "news-releases") and page == 0:
                return Response(200, {"Content-Type": "text/html"}, fixture["listing"], url)
            if slug in ("feature-stories", "news-releases", "in-the-news"):
                return Response(200, {"Content-Type": "text/html"}, fixture["empty"], url)
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
        self.assertIn(result["status"], ("complete", "complete_unverified"), result)
        self.assertEqual(["1999", "576"], sorted(d["post_number"] for d in docs))
        urls = [c[1] for c in transport.calls]
        self.assertIn(BASE + "/news/news-releases", urls)
        self.assertIn(BASE + "/news/in-the-news", urls)
        self.assertIn(BASE + "/news/feature-stories?page=1", urls)
        self.assertNotIn(BASE + "/news/news-releases?page=0", urls)


if __name__ == "__main__":
    unittest.main()
