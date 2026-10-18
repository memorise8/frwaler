"""sandia-gov-news: two fixed source pages (ext:multi_listing explicit segments with their own
first pages = crawl()'s two URLs) whose PDF links become records; abstract from pdftotext -f 1
-l 2 (ext:pdf_text, min_bytes 1000 = crawl()'s small-download rule) with crawl()'s synthesized
fallback. The split methods must give crawl()'s exact Paper on the same bytes. No network:
crawl()'s curl calls are answered from the fixture, pdftotext runs locally in both paths."""
import ast
import contextlib
import copy
import hashlib
import io
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import ROOT, load, split_fixture, split_plan
from tests.pdf_text_fixture import make_pdf

SITE = "sandia-gov-news"
MAIN = "https://www.sandia.gov/news/publications/environmental-reports/"
ARCHIVE = MAIN + "env-archive/"
EXPECTED_IDS = ["2024_ASER_NM.pdf", "2024_ASER_KTF_small.pdf", "2023_ASER_NM_Summary.pdf", "Groundwater_2019.PDF"]


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
        self.pdfs = {url: value.encode() if isinstance(value, str) else make_pdf(value)
                     for url, value in self.fixture["pdfs"].items()}
        self.denials = []

        def denied(*args, **kwargs):
            self.denials.append("attempted network I/O")
            raise AssertionError("split test attempted network I/O")
        for name in ("socket.getaddrinfo", "socket.socket.connect", "socket.socket.connect_ex"):
            p = patch(name, denied)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: self.assertEqual([], self.denials))

    def native(self):
        """Unchanged crawl(): source pages from the fixture, curl -o answered from the fixture
        PDFs, its own pdftotext command run for real."""
        crawler, module = load(SITE)
        captured = []
        real_run = subprocess.run

        def run(cmd, *args, **kwargs):
            if cmd[0] == "curl":
                Path(cmd[cmd.index("-o") + 1]).write_bytes(self.pdfs[cmd[-1]])
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
            self.assertEqual("pdftotext", cmd[0])
            return real_run(cmd, *args, **kwargs)
        crawler._curl_get = lambda url, retries=3, timeout=30: self.fixture["pages"][url]
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch.object(module.subprocess, "run", run), patch("time.sleep", lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        return captured

    def pipeline(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section("adapter"), None, crawler=crawler)
        parser = ListingParser(p.listing["parser"], adapter=adapter)
        firsts = [s["first_page_url"] for s in p.section("segments")["items"]]
        self.assertEqual([MAIN, ARCHIVE], firsts)
        papers = []
        for first in firsts:
            items = parser.parse(Response(200, {"Content-Type": "text/html"}, self.fixture["pages"][first], first)).items
            for item in items:
                paper = adapter.materialize(item, SimpleNamespace(
                    get=lambda u: Response(200, {"Content-Type": "application/pdf"}, self.pdfs[u], u)))
                if paper is not None:
                    papers.append(paper)
        return papers


class SandiaSplitTest(Base):
    def test_full_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(4, len(expected))
        actual = self.pipeline()
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])
        self.assertEqual(EXPECTED_IDS, [paper_identity(p)[0] for p in actual])
        self.assertEqual([MAIN, MAIN, ARCHIVE, ARCHIVE], [paper_identity(p)[1] for p in actual])
        self.assertIn("Monitoring line 29", actual[0]["abstract"])        # pages 1-2 read
        self.assertNotIn("Page three", actual[0]["abstract"])             # -l 2
        self.assertTrue(actual[1]["abstract"].startswith("The 2024 Annual Site Environmental Report"))  # < 1000 bytes
        self.assertTrue(actual[3]["abstract"].startswith("The 2019 Annual Groundwater"))  # unreadable -> synthesized
        self.assertEqual("2023-01-01", actual[2]["published_date"])      # year from the title

    def test_pdf_text_options_are_crawls_pdftotext_command(self):
        code = (ROOT / "crawler/sites/custom" / (SITE + ".py")).read_text()
        self.assertIn('["pdftotext", "-f", "1", "-l", "2", tmp_path, "-"]', code)
        self.assertIn("os.path.getsize(tmp_path) < 1000", code)
        spec = split_plan(SITE)["adapter"]["pdf_text"]
        self.assertEqual({"args": ["-f", "1", "-l", "2"], "timeout_s": 30, "min_bytes": 1000}, spec)

    def test_original_methods_are_unchanged(self):
        code = (ROOT / "crawler/sites/custom" / (SITE + ".py")).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fixture["original_method_source_sha256"].items():
            self.assertEqual(digest, actual[name], name)

    def test_shipped_plan_completes_end_to_end(self):
        """Real SiteRunner: both source pages (page/2/ repeats page 1 like WordPress does,
        which ends each segment), PDFs through the framework pdftotext."""
        from tests.enumeration_fakes import Env
        pages, pdfs = self.fixture["pages"], self.pdfs

        def handle(method, url, data):
            path = urlsplit(url).path
            if path == "/robots.txt":
                return Response(404, {}, b"", url)
            for first in (ARCHIVE, MAIN):
                if url == first or url.startswith(first + "page/"):
                    return Response(200, {"Content-Type": "text/html"}, pages[first], url)
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
        self.assertIn(result["status"], ("complete", "complete_unverified"), result)
        self.assertEqual(sorted(EXPECTED_IDS), sorted(d["post_number"] for d in docs))


if __name__ == "__main__":
    unittest.main()
