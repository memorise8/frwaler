"""ND-2 (NR-20261018): shared memory-only checks for one-page listings whose rows are the
papers crawl() saves (list_returns_paper / paper_method, no detail request). crawl() is
unchanged; its save argument is compared with the framework pipeline. No network,
subprocess or database is used (sqlite only inside tests.enumeration_fakes.Env for runs)."""
import ast
import contextlib
import copy
import hashlib
import io
import json
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from crawler.enumeration.urls import build_url
from tests.collector_split_helpers import ROOT, load, run_split, split_fixture, split_plan


class OnePageListChecks:
    SITE = None
    TRANSPORTS = ('_curl_get', '_curl', '_fetch', '_get', '_fetch_page')
    EXPECT = 2

    def setUp(self):
        self.fixture = split_fixture(self.SITE)
        self.plan = {'version': 1, 'sites': {self.SITE: split_plan(self.SITE)}}
        self.denials = []

        def denied(*a, **k):
            self.denials.append('io')
            raise AssertionError('attempted network/process I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'socket.socket.connect_ex',
                     'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)
        self.addCleanup(lambda: self.assertEqual([], self.denials))

    def native(self, body=None):
        crawler, module = load(self.SITE)
        captured, list_calls = [], []

        def transport(url, *a, **k):
            list_calls.append(url)
            return (self.fixture['listing_body'] if body is None else body) if len(list_calls) == 1 \
                else self.fixture['empty_body']
        crawler._delay = 0
        crawler._source_failure = None
        crawler.delivery_cursor = None
        crawler._check_cancelled = lambda: None
        crawler._advance_cursor = lambda *a, **k: None
        crawler._mark_exhausted = lambda: None
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with contextlib.ExitStack() as stack:
            for name in self.TRANSPORTS:
                if callable(getattr(type(crawler), name, None)):
                    stack.enter_context(patch.object(crawler, name, transport, create=True))
                if callable(getattr(module, name, None)):
                    stack.enter_context(patch.object(module, name, transport))
            stack.enter_context(patch('time.sleep', lambda *a: None))
            with contextlib.redirect_stdout(io.StringIO()):
                crawler.crawl(limit=None)
        return captured

    def pipeline(self, body=None):
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        adapter = Adapter(self.SITE, p.section('adapter'), None, crawler=crawler)
        url = build_url(p.listing['url'], p.listing.get('params') or {})
        resp = Response(200, {'Content-Type': self.fixture['content_type']},
                        self.fixture['listing_body'] if body is None else body, url)
        items = ListingParser(p.listing['parser'], adapter=adapter).parse(resp).items
        out = []
        for item in items:
            paper = adapter.materialize(item, None)
            if paper is not None:
                out.append(paper)
        return out

    def test_papers_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(self.EXPECT, len(expected))
        actual = self.pipeline()
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])

    def test_original_methods_unchanged(self):
        code = (ROOT / 'crawler/sites/custom' / (self.SITE + '.py')).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fixture['original_method_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)

    def test_error_page_is_parse_error(self):
        c, _ = load(self.SITE)
        for bad in ({'error': 'x'}, '<html><body>maintenance</body></html>'):
            with self.assertRaises(PermanentFailure) as ctx:
                with guarded():
                    c._enumeration_items(bad)
            self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_caller_input_not_mutated(self):
        before = copy.deepcopy(self.fixture)
        self.pipeline()
        self.assertEqual(before, self.fixture)

    def run_wrap(self):
        """Real SiteRunner.run: every page number serves the same one page (wrap = end)."""
        fx = self.fixture
        return run_split(self.SITE, end=lambda url: Response(200, {'Content-Type': fx['content_type']},
                                                               fx['listing_body'], url))
