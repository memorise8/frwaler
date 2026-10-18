"""graduateinstitute-ch-library: www list cards -> www detail page -> its repository CTA link
rewritten to the MARCXML export (ext:two_step_keep_first + ext:second_step_link_rewrite), short
abstracts skipped by crawl()'s rule (ext:none_is_rule_skip). The split methods must give crawl()'s
exact Paper on the same bytes. No network: crawl()'s curl calls are answered from the fixture."""
import ast
import contextlib
import copy
import hashlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import ROOT, load, split_fixture, split_plan

SITE = "graduateinstitute-ch-library"
START = "https://www.graduateinstitute.ch/library/publications-institute"
CARD_URLS = [START + "/" + s for s in ("cyber-scams", "animal-geometry", "card-record", "short-one", "no-cta")]


class Base(unittest.TestCase):
    def setUp(self):
        self.fixture = split_fixture(SITE)
        self.plan = {"version": 1, "sites": {SITE: split_plan(SITE)}}
        self.bodies = {**self.fixture["details"], **self.fixture["marcs"]}
        self.denials = []

        def denied(*args, **kwargs):
            self.denials.append("attempted network I/O")
            raise AssertionError("split test attempted network I/O")
        for name in ("socket.getaddrinfo", "socket.socket.connect", "socket.socket.connect_ex"):
            p = patch(name, denied)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: self.assertEqual([], self.denials))

    def native(self, bodies=None):
        """Unchanged crawl(): list page 0 from the fixture, every other curl call from ``bodies``."""
        bodies = self.bodies if bodies is None else bodies
        crawler, module = load(SITE)
        captured, calls = [], []

        def curl_get(url, **kwargs):
            calls.append(url)
            if url == START:
                return self.fixture["listing_body"]
            return bodies.get(url)
        crawler._curl_get = curl_get
        crawler._detail_delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch("time.sleep", lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertNotIn(START + "/card-record", calls)  # card record id: crawl() goes straight to MARC
        return captured

    def adapter_items(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section("adapter"), None, crawler=crawler)
        resp = Response(200, {"Content-Type": "text/html"}, self.fixture["listing_body"].encode(), START)
        items = ListingParser(p.listing["parser"], adapter=adapter).parse(resp).items
        return adapter, items

    def pipeline(self, bodies=None):
        bodies = self.bodies if bodies is None else bodies
        adapter, items = self.adapter_items()
        self.assertEqual(CARD_URLS, [i.key for i in items])
        requested = []

        def fetch(url):
            requested.append(url)
            self.assertIn(url, bodies, "undeclared detail request")
            kind = "application/xml" if "of=xm" in url else "text/html"
            return Response(200, {"Content-Type": kind}, bodies[url].encode("utf-8"), url)
        papers = []
        with contextlib.redirect_stdout(io.StringIO()):
            for item in items:
                paper = adapter.materialize(item, SimpleNamespace(get=fetch))
                if paper is not None:
                    papers.append(paper)
        return papers, requested


class GraduateInstituteSplitTest(Base):
    def test_full_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(4, len(expected))  # short-one: MARC abstract under 100 characters, skipped by both
        actual, requested = self.pipeline()
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])
        self.assertEqual(["322225", "322226", "300001", "15"], [paper_identity(p)[0] for p in actual])
        # The CTA link http://repository.../record/<id> is rewritten to the https MARCXML export.
        self.assertIn("https://repository.graduateinstitute.ch/record/322225?of=xm", requested)
        self.assertFalse(any(u.startswith("http://") for u in requested))
        # MARC without abstract -> www abstract (crawl()'s fallback), no CTA -> www page only.
        self.assertIn("geometry of meaning", actual[1]["abstract"])
        self.assertEqual("2025-01-01", actual[3]["published_date"])

    def test_a_different_marc_record_is_refused_not_stored(self):
        """The card says record 300001; if the page's CTA pointed elsewhere the method raises."""
        bodies = dict(self.bodies)
        url = START + "/card-record"
        bodies[url] = bodies[url].replace("/record/300001", "/record/322225")
        adapter, items = self.adapter_items()
        item = next(i for i in items if i.url == url)
        fetch = SimpleNamespace(get=lambda u: Response(200, {}, bodies[u].encode(), u))
        with self.assertRaises(ValueError) as ctx, contextlib.redirect_stdout(io.StringIO()):
            adapter.materialize(item, fetch)
        self.assertIn("crawl() reads MARCXML of record 300001", str(ctx.exception))

    def test_short_abstract_is_a_rule_skip(self):
        adapter, items = self.adapter_items()
        item = next(i for i in items if i.url.endswith("/short-one"))
        fetch = SimpleNamespace(get=lambda u: Response(200, {}, self.bodies[u].encode(), u))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(adapter.materialize(item, fetch))
        self.assertTrue(split_plan(SITE)["adapter"]["none_is_rule_skip"])

    def test_original_methods_are_unchanged(self):
        code = (ROOT / "crawler/sites/custom" / (SITE + ".py")).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        self.assertIn("crawl", self.fixture["original_method_source_sha256"])
        for name, digest in self.fixture["original_method_source_sha256"].items():
            self.assertEqual(digest, actual[name], name)

    def test_list_urls_are_crawls(self):
        crawler, _ = load(SITE)
        listing = split_plan(SITE)["listing"]
        self.assertEqual(crawler._list_url(0), listing["first_page_url"])
        self.assertEqual(crawler._list_url(3), listing["url"].replace("{page}", "3"))

    def test_shipped_plan_completes_end_to_end(self):
        """Real SiteRunner: robots on both hosts, list page 0 at the crawl start URL, past the
        end 404, www detail then rewritten MARCXML; the short abstract is ledgered as a rule skip."""
        from tests.enumeration_fakes import Env
        fixture, bodies = self.fixture, self.bodies

        def handle(url):
            if urlsplit(url).path == "/robots.txt":
                return Response(404, {}, b"", url)
            if url == START:
                return Response(200, {"Content-Type": "text/html"}, fixture["listing_body"].encode(), url)
            if url in bodies:
                kind = "application/xml" if "of=xm" in url else "text/html"
                return Response(200, {"Content-Type": kind}, bodies[url].encode("utf-8"), url)
            return Response(404, {}, b"", url)
        site = dict(split_plan(SITE), request_interval_s=0)
        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, "site_id", None) == SITE)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(site, handle, crawler=cls(env.conn))
            docs = env.docs()
        finally:
            env.close()
        self.assertEqual(1, result["stats"].get("skipped_by_rule"), result)
        self.assertEqual(4, result["stats"]["saved_new"], result)
        self.assertEqual(["15", "300001", "322225", "322226"], sorted(d["post_number"] for d in docs))
        self.assertTrue(result["status"].startswith("complete"), result)


if __name__ == "__main__":
    unittest.main()
