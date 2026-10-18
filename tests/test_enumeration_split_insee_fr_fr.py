"""insee-fr-fr: Solr POST listing (ext:post_listing, Accept: application/json via ext:headers),
detail page -> PDF (ext:two_step_keep_first) -> abstract from pdftotext (ext:pdf_text with
crawl()'s -layout -enc UTF-8 -f 1 -l 4). The split methods must give crawl()'s exact Paper on the
same bytes. No network: crawl()'s curl calls are answered from the fixture, pdftotext runs
locally on generated PDFs in both paths."""
import ast
import contextlib
import copy
import hashlib
import io
import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import header_hosts, resolve_headers, site_plan
from tests.collector_split_helpers import ROOT, load, split_fixture, split_plan
from tests.pdf_text_fixture import make_pdf

SITE = "insee-fr-fr"
SOLR = "https://www.insee.fr/fr/solr/consultation"


class RecordingTransport:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, method, url, *, headers=None, data=None):
        self.calls.append((method, url, headers))
        return self.handler(method, url, data)


class Base(unittest.TestCase):
    def setUp(self):
        self.fixture = split_fixture(SITE)
        self.plan = {"version": 1, "sites": {SITE: split_plan(SITE)}}
        self.bodies = {url: html.encode("utf-8") for url, html in self.fixture["details"].items()}
        self.bodies.update({url: make_pdf(pages) for url, pages in self.fixture["pdfs"].items()})
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
        """Unchanged crawl(): Solr answer from the fixture, curl answered from ``bodies``,
        its own pdftotext commands run for real."""
        bodies = self.bodies if bodies is None else bodies
        crawler, module = load(SITE)
        captured, posts = [], []
        real_run = subprocess.run

        def solr_post(payload, context="Solr"):
            posts.append(payload)
            return self.fixture["listing_body"] if len(posts) == 1 else '{"documents": []}'

        def run(cmd, *args, **kwargs):
            if cmd[0] == "curl":
                return SimpleNamespace(returncode=0, stdout=bodies[cmd[-1]], stderr=b"")
            self.assertEqual("pdftotext", cmd[0])
            return real_run(cmd, *args, **kwargs)
        crawler._solr_post = solr_post
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch.object(module.subprocess, "run", run), patch("time.sleep", lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertEqual([0, 20], [p["start"] for p in posts])
        return captured

    def pipeline(self, bodies=None):
        bodies = self.bodies if bodies is None else bodies
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section("adapter"), None, crawler=crawler)
        resp = Response(200, {"Content-Type": "application/json"}, self.fixture["listing_body"], SOLR)
        items = ListingParser(p.listing["parser"], adapter=adapter).parse(resp).items
        self.assertEqual(["9085592", "9088692", "9000001", "9000002"], [i.key for i in items])

        def fetch(url):
            self.assertIn(url, bodies, "undeclared detail request")
            kind = "application/pdf" if url.endswith(".pdf") else "text/html"
            return Response(200, {"Content-Type": kind}, bodies[url], url)
        papers = []
        for item in items:
            paper = adapter.materialize(item, SimpleNamespace(get=fetch))
            if paper is not None:
                papers.append(paper)
        return papers


class InseeSplitTest(Base):
    def test_full_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(2, len(expected))  # no PDF link / too-short PDF text: skipped by both
        actual = self.pipeline()
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])
        self.assertEqual(["9085592", "9088692"], [paper_identity(p)[0] for p in actual])
        self.assertNotIn("Page cinq", actual[0]["abstract"])  # -l 4 like crawl()
        self.assertIn("Page quatre", actual[0]["abstract"])

    def test_a_different_pdf_link_is_refused_not_stored(self):
        """crawl() prefers the 'Version imprimable' block; if the framework's selector picked
        another PDF the method raises instead of saving that PDF's text."""
        bodies = dict(self.bodies)
        url = "https://www.insee.fr/fr/information/9085592"
        other = "https://www.insee.fr/fr/statistiques/fichier/9085592/annexe.pdf"
        bodies[url] = bodies[url].replace(b"<body>", b'<body><a href="/fr/statistiques/fichier/9085592/annexe.pdf">x</a>')
        bodies[other] = make_pdf([["Annexe " * 30]])
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section("adapter"), None, crawler=crawler)
        resp = Response(200, {"Content-Type": "application/json"}, self.fixture["listing_body"], SOLR)
        item = ListingParser(p.listing["parser"], adapter=adapter).parse(resp).items[0]
        fetch = SimpleNamespace(get=lambda u: Response(200, {}, bodies[u], u))
        with self.assertRaises(Exception) as ctx:
            adapter.materialize(item, fetch)
        self.assertIn("crawl() reads the PDF", str(ctx.exception))

    def test_listing_shape_errors(self):
        crawler, _ = load(SITE)
        with self.assertRaises(PermanentFailure):
            crawler._enumeration_items({"documents": {"x": 1}}, SOLR)
        self.assertEqual([], crawler._enumeration_items({"numFounds": 5380, "documents": []}, SOLR))

    def test_pdf_text_options_are_crawls_first_pdftotext_command(self):
        code = (ROOT / "crawler/sites/custom" / (SITE + ".py")).read_text()
        self.assertIn('"pdftotext", "-layout", "-enc", "UTF-8",\n                "-f", "1", "-l", str(self.PDF_PAGES), "-", "-",',
                      code)
        crawler, _ = load(SITE)
        self.assertEqual((4, 45), (crawler.PDF_PAGES, crawler.PDF_TIMEOUT))
        spec = split_plan(SITE)["adapter"]["pdf_text"]
        self.assertEqual({"args": ["-layout", "-enc", "UTF-8", "-f", "1", "-l", "4"], "timeout_s": 45, "min_bytes": 0},
                         spec)

    def test_original_methods_are_unchanged(self):
        code = (ROOT / "crawler/sites/custom" / (SITE + ".py")).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fixture["original_method_source_sha256"].items():
            self.assertEqual(digest, actual[name], name)

    def test_shipped_plan_completes_end_to_end(self):
        """Real SiteRunner: robots, POST listing offsets 0.. (Accept: application/json),
        empty documents past the end, detail page then PDF, framework pdftotext."""
        from tests.enumeration_fakes import Env
        fixture, bodies = self.fixture, self.bodies
        starts = []

        def handle(method, url, data):
            if urlsplit(url).path == "/robots.txt":
                return Response(404, {}, b"", url)
            if url == SOLR:
                self.assertEqual("POST", method)
                body = json.loads(data)
                self.assertEqual("communiquesDePresse", body["filters"][0]["values"][0])
                starts.append(body.get("start", 0))
                text = fixture["listing_body"] if body.get("start", 0) == 0 else '{"numFounds": 6, "documents": []}'
                return Response(200, {"Content-Type": "application/json"}, text, url)
            if url in bodies:
                return Response(200, {"Content-Type": "application/pdf" if url.endswith(".pdf") else "text/html"},
                                bodies[url], url)
            return Response(404, {}, b"", url)
        site = dict(split_plan(SITE), request_interval_s=0)
        env = Env(SITE)
        transport = RecordingTransport(handle)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, "site_id", None) == SITE)
            with contextlib.redirect_stdout(io.StringIO()):
                # Same header wiring as run.make_fetcher (Env.fetcher does not read the plan's headers).
                result = env.run(site, None, crawler=cls(env.conn), transport=transport,
                                 extra_headers=resolve_headers(site), header_hosts=header_hosts(site))
            docs = env.docs()
        finally:
            env.close()
        # crawl()'s two skips (no PDF link, too-short PDF text) are ledgered as empty items for
        # review on a contract site; the enumeration itself completes.
        self.assertEqual("complete_with_review", result["status"], result)
        self.assertEqual("adapter_contract_failures:2", result["stop_reason"])
        self.assertEqual((2, 2), (result["stats"]["saved_new"], result["stats"]["failed_permanent"]))
        self.assertEqual(["9085592", "9088692"], sorted(d["post_number"] for d in docs))
        self.assertIn(0, starts)
        solr_headers = [h for m, u, h in transport.calls if u == SOLR]
        self.assertTrue(solr_headers and all((h or {}).get("Accept") == "application/json" for h in solr_headers))


if __name__ == "__main__":
    unittest.main()
