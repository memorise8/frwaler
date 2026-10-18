"""transformation-gouv-fr-espace-presse: list cards link straight to PDFs; the abstract is the
first 3000 characters of ``pdftotext <file> -`` (ext:pdf_text, plan args []). The split methods
must give crawl()'s exact Paper on the same PDF bytes. No network: crawl()'s curl calls are
answered from the fixture, pdftotext runs locally on generated PDFs in both paths."""
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
from urllib.parse import parse_qs, urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import ROOT, load, split_fixture, split_plan
from tests.pdf_text_fixture import make_pdf

SITE = "transformation-gouv-fr-espace-presse"
LIST = "https://www.transformation.gouv.fr/espace-presse?field_type_presse_target_id%5B17%5D=17"


class RecordingTransport:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, method, url, *, headers=None, data=None):
        self.calls.append((method, url))
        return self.handler(method, url, data)


class Base(unittest.TestCase):
    def setUp(self):
        self.fixture = split_fixture(SITE)
        self.plan = {"version": 1, "sites": {SITE: split_plan(SITE)}}
        self.pdfs = {url: make_pdf(pages) for url, pages in self.fixture["pdfs"].items()}
        self.denials = []

        def denied(*args, **kwargs):
            self.denials.append("attempted network I/O")
            raise AssertionError("split test attempted network I/O")
        for name in ("socket.getaddrinfo", "socket.socket.connect", "socket.socket.connect_ex"):
            p = patch(name, denied)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: self.assertEqual([], self.denials))

    def native(self, pdfs=None):
        """Unchanged crawl(): list page from the fixture, curl answered from ``pdfs``,
        its own pdftotext command run for real."""
        pdfs = self.pdfs if pdfs is None else pdfs
        crawler, module = load(SITE)
        captured, list_calls = [], []
        real_run = subprocess.run

        def curl_text(url, retries=3):
            list_calls.append(url)
            return self.fixture["listing_body"] if len(list_calls) == 1 else None

        def run(cmd, *args, **kwargs):
            if cmd[0] == "curl":
                return SimpleNamespace(returncode=0, stdout=pdfs[cmd[-1]], stderr=b"")
            self.assertEqual("pdftotext", cmd[0])
            return real_run(cmd, *args, **kwargs)
        crawler._curl_text = curl_text
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch.object(module.subprocess, "run", run), patch("time.sleep", lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertEqual([LIST], list_calls[:1])
        return captured

    def pipeline(self, pdfs=None):
        pdfs = self.pdfs if pdfs is None else pdfs
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section("adapter"), None, crawler=crawler)
        resp = Response(200, {"Content-Type": "text/html"}, self.fixture["listing_body"], LIST + "&page=0")
        items = ListingParser(p.listing["parser"], adapter=adapter).parse(resp).items
        self.assertEqual(3, len(items))

        def fetch(url):
            self.assertIn(url, pdfs, "undeclared detail request")
            return Response(200, {"Content-Type": "application/pdf"}, pdfs[url], url)
        papers = []
        for item in items:
            paper = adapter.materialize(item, SimpleNamespace(get=fetch))
            if paper is not None:
                papers.append(paper)
        return papers


class TransformationSplitTest(Base):
    def test_full_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(2, len(expected))  # the short PDF is skipped by both
        actual = self.pipeline()
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])
        self.assertEqual(["Communiqué-Synthetic_plan-canicule.pdf",
                          "COMMUNIQUÉ DE PRESSE - Synthetic two.pdf"], [paper_identity(p)[0] for p in actual])
        self.assertEqual(3000, len(actual[0]["abstract"]))  # crawl()'s 3000-character cut

    def test_unreadable_pdf_is_skipped_like_crawl(self):
        pdfs = dict(self.pdfs)
        first = next(iter(pdfs))
        pdfs[first] = b"<html>not a pdf</html>"  # pdftotext exits non-zero in both paths
        self.assertEqual(1, len(self.native(pdfs)))
        self.assertEqual(1, len(self.pipeline(pdfs)))

    def test_pdf_text_options_are_crawls_pdftotext_command(self):
        code = (ROOT / "crawler/sites/custom" / (SITE + ".py")).read_text()
        self.assertIn('["pdftotext", tmp, "-"]', code)  # no options, 30 s timeout
        self.assertIn("timeout=30", code.split("def _pdf_text", 1)[1].split("def ", 1)[0])
        spec = split_plan(SITE)["adapter"]["pdf_text"]
        self.assertEqual({"args": [], "timeout_s": 30, "min_bytes": 0}, spec)

    def test_original_methods_are_unchanged(self):
        code = (ROOT / "crawler/sites/custom" / (SITE + ".py")).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fixture["original_method_source_sha256"].items():
            self.assertEqual(digest, actual[name], name)

    def test_shipped_plan_completes_end_to_end(self):
        """Real SiteRunner: robots, list pages from 0 (page-less first page too), the empty
        'Il n'y a aucun résultat' page past the end, PDF details with framework pdftotext."""
        from tests.enumeration_fakes import Env
        fixture, pdfs = self.fixture, self.pdfs

        def handle(method, url, data):
            parts = urlsplit(url)
            if parts.path == "/robots.txt":
                return Response(404, {}, b"", url)
            if parts.path == "/espace-presse":
                page = int(parse_qs(parts.query).get("page", ["0"])[0])
                body = fixture["listing_body"] if page == 0 else fixture["empty_body"]
                return Response(200, {"Content-Type": "text/html"}, body, url)
            if url in pdfs:
                return Response(200, {"Content-Type": "application/pdf"}, pdfs[url], url)
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
        self.assertEqual(["COMMUNIQUÉ DE PRESSE - Synthetic two.pdf", "Communiqué-Synthetic_plan-canicule.pdf"],
                         sorted(d["post_number"] for d in docs))


if __name__ == "__main__":
    unittest.main()
