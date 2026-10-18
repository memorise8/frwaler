"""academie-sciences-fr-espace-presse: Drupal press list (PDF rows) plus the three editorial
sections on page one (PDF and HTML article cards). PDF rows: paper from pdftotext -layout
(ext:pdf_text) through crawl()'s own _prepare_item. The split methods must give crawl()'s exact
rows and Paper on the same bytes. No network: crawl()'s fetch helpers answer from the fixture,
pdftotext runs locally on generated PDFs in both paths. (crawl() itself is driven by the delivery
database cursor, so it is compared at its per-page (_ac_page) and per-item (_prepare_item) seams.)"""
import ast
import contextlib
import copy
import hashlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from crawler.academie_source import BASE, START
from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import ROOT, load, split_fixture, split_plan
from tests.pdf_text_fixture import make_pdf
from tests.test_academie_press_sections import article, listing

SITE = "academie-sciences-fr-espace-presse"
SAVED = ["files/1.pdf", "article", "column", "files/dossier.pdf", "files/3.pdf"]


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
        self.bodies = {url: make_pdf(pages) for url, pages in self.fixture["pdfs"].items()}
        for path in ("/article", "/column"):
            self.bodies[BASE + path] = article(BASE + path, newspaper=path == "/column").encode()
        self.denials = []

        def denied(*args, **kwargs):
            self.denials.append("attempted network I/O")
            raise AssertionError("split test attempted network I/O")
        for name in ("socket.getaddrinfo", "socket.socket.connect", "socket.socket.connect_ex"):
            p = patch(name, denied)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: self.assertEqual([], self.denials))

    def native_crawler(self, bodies=None):
        bodies = self.bodies if bodies is None else bodies
        crawler, _ = load(SITE)
        crawler._source_failure = None

        def fetch_text(url, **kw):
            if url in bodies:
                return bodies[url].decode()
            return listing(int(parse_qs(urlsplit(url).query).get("page", ["0"])[0]))
        crawler._fetch_text = fetch_text
        crawler._fetch_bytes = lambda url, **kw: bodies[url]
        return crawler  # the original _extract_pdf_text (real pdftotext -layout) is kept

    def native(self, bodies=None):
        crawler = self.native_crawler(bodies)
        papers = []
        with contextlib.redirect_stdout(io.StringIO()):
            for page in (0, 1):
                for item in crawler._ac_page(page)["items"]:
                    paper = crawler._prepare_item(item["native"])
                    if paper is not None:
                        papers.append(copy.deepcopy(paper))
        return papers

    def pipeline(self, bodies=None):
        bodies = self.bodies if bodies is None else bodies
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section("adapter"), None, crawler=crawler)
        parser = ListingParser(p.listing["parser"], adapter=adapter)
        papers = []
        for page in (0, 1):
            url = f"{START}?page={page}"
            items = parser.parse(Response(200, {"Content-Type": "text/html"}, listing(page), url)).items
            for item in items:
                with contextlib.redirect_stdout(io.StringIO()):
                    paper = adapter.materialize(item, SimpleNamespace(get=lambda u: Response(200, {}, bodies[u], u)))
                if paper is not None:
                    papers.append(paper)
        return papers


class AcademieSplitTest(Base):
    def test_rows_equal_crawls_page_reader(self):
        crawler = self.native_crawler()
        for page, url in ((0, START), (0, START + "?page=0"), (1, START + "?page=1"), (2, START + "?page=2")):
            expected = crawler._ac_page(page)["items"]
            rows = crawler._enumeration_items(listing(page), url)
            self.assertEqual([(i["key"], i["url"], i["native"]) for i in expected],
                             [(r["key"], r["url"], r["native"]) for r in rows], url)
        self.assertEqual([], crawler._enumeration_items(listing(2), START + "?page=2"))  # end page

    def test_full_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(5, len(expected))  # files/2.pdf: title-only body, excluded by both
        actual = self.pipeline()
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])
        self.assertEqual(SAVED, [paper_identity(p)[0] for p in actual])
        self.assertEqual("Paris, le 6 octobre 2026", actual[0]["published_date"])
        self.assertNotIn("non retenue", actual[0]["abstract"])

    def test_non_pdf_body_fails_like_crawl(self):
        bodies = dict(self.bodies)
        bodies[BASE + "/files/1.pdf"] = b"<html>error</html>"
        crawler = self.native_crawler(bodies)
        item = crawler._ac_page(0)["items"][0]
        with self.assertRaises(ValueError):
            crawler._prepare_item(item["native"])
        with self.assertRaises(ValueError):
            crawler._enumeration_detail(bodies[BASE + "/files/1.pdf"], None, item)

    def test_pdf_text_options_are_crawls_pdftotext_command(self):
        code = (ROOT / "crawler/sites/custom" / (SITE + ".py")).read_text()
        body = code.split("    def _extract_pdf_text(self, pdf_bytes, url):", 1)[1].split("\n    def ", 1)[0]
        self.assertIn('["pdftotext", "-layout", "-", "-"]', body)
        self.assertIn("timeout=90", body)
        spec = split_plan(SITE)["adapter"]["pdf_text"]
        self.assertEqual({"args": ["-layout"], "timeout_s": 90, "min_bytes": 0}, spec)

    def test_split_restores_the_crawler_helpers(self):
        crawler = self.native_crawler()
        crawler.__dict__.pop("_fetch_text"); crawler.__dict__.pop("_fetch_bytes")
        item = crawler._enumeration_items(listing(1), START + "?page=1")[0]
        crawler._enumeration_detail(self.bodies[item["url"]], "x " * 10, item)
        for name in ("_fetch_text", "_fetch_bytes", "_extract_pdf_text"):
            self.assertNotIn(name, crawler.__dict__)

    def test_original_methods_are_unchanged(self):
        code = (ROOT / "crawler/sites/custom" / (SITE + ".py")).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fixture["original_method_source_sha256"].items():
            self.assertEqual(digest, actual[name], name)

    def test_shipped_plan_completes_end_to_end(self):
        """Real SiteRunner: list pages from 0 (page-less first page too), the empty Drupal view
        past the end (listing_marker), PDF and article details."""
        from tests.enumeration_fakes import Env
        bodies = self.bodies

        def handle(method, url, data):
            parts = urlsplit(url)
            if parts.path == "/robots.txt":
                return Response(404, {}, b"", url)
            if parts.path == "/espace-presse":
                page = int(parse_qs(parts.query).get("page", ["0"])[0])
                return Response(200, {"Content-Type": "text/html"}, listing(page), url)
            if url in bodies:
                return Response(200, {"Content-Type": "application/pdf" if url.endswith(".pdf") else "text/html"},
                                bodies[url], url)
            return Response(404, {}, b"", url)
        site = dict(split_plan(SITE), request_interval_s=0)
        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, "site_id", None) == SITE)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(site, None, crawler=cls(env.conn, delay=0), transport=RecordingTransport(handle))
            docs = env.docs()
        finally:
            env.close()
        self.assertIn(result["status"], ("complete", "complete_with_review"), result)
        self.assertEqual(sorted(SAVED), sorted(d["post_number"] for d in docs))


if __name__ == "__main__":
    unittest.main()
