"""Memory-only regression: compare adapter output with unchanged crawl's save argument.
No BaseCrawler constructor, database, real transport, or subprocess is used.
"""
import ast
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan

ROOT = Path(__file__).resolve().parents[1]


def load(site):
    path = ROOT / 'crawler/sites/custom' / (site + '.py')
    spec = importlib.util.spec_from_file_location('split_test_' + site.replace('-', '_'), path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    old_path = sys.path[:]
    try:
        with guarded():
            spec.loader.exec_module(module)
    finally:
        sys.path[:] = old_path
    cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == site)
    return object.__new__(cls), module


class SplitChecks:
    def setUp(self):
        self.fixture = json.loads((ROOT/'tests/fixtures/collector_splits'/(self.SITE+'.json')).read_text())
        self.plan = json.loads((ROOT/'docs/examples/collector-splits'/(self.SITE+'.json')).read_text())
        self.denials = []
        with guarded():
            pass
        def denied(*args, **kwargs):
            self.denials.append('attempted I/O')
            raise AssertionError('split fixture attempted network/process/database I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'socket.socket.connect_ex',
                     'subprocess.Popen._execute_child', 'sqlite3.connect'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)
        for name in ('psycopg', 'psycopg2'):
            try:
                mod = __import__(name)
            except ImportError:
                continue
            p = patch.object(mod, 'connect', denied); p.start(); self.addCleanup(p.stop)
        self.addCleanup(lambda: self.assertEqual([], self.denials))

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, module = load(self.SITE)
        captured = []; calls = []; list_calls = []
        def transport(url, *args, **kwargs):
            calls.append(url)
            if url in fixture['details']:
                value = fixture['details'][url]
                return json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value
            list_calls.append(url)
            if len(list_calls) == 1:
                return fixture['listing_body']
            return '[]' if fixture['content_type']=='application/json' else '<rss><channel/></rss>' if 'rss' in fixture['content_type'] else '<html><main/></html>'
        crawler._delay = 0
        crawler._detail_delay = 0
        crawler._last_save_created = True
        crawler._source_failure = None
        crawler.delivery_cursor = None
        crawler._check_cancelled = lambda: None
        crawler._advance_cursor = lambda *a, **k: None
        crawler._mark_exhausted = lambda: None
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_get = transport
        crawler._session = SimpleNamespace(get=lambda url, **kw: SimpleNamespace(
            status_code=200, content=transport(url).encode(), raise_for_status=lambda: None))
        with contextlib.ExitStack() as stack:
            if hasattr(module, '_curl_get'):
                stack.enter_context(patch.object(module, '_curl_get', transport))
            stack.enter_context(patch('time.sleep', lambda *a: None))
            with contextlib.redirect_stdout(io.StringIO()):
                result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertLessEqual(len(list_calls), 2)
        return captured, calls

    def pipeline(self, fixture=None, redirect=False):
        fixture = fixture or self.fixture
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        adapter = Adapter(self.SITE, p.section('adapter'), None, crawler=crawler)
        url = p.listing['url']
        if p.listing.get('params'):
            url += ('&' if '?' in url else '?') + urlencode(p.listing['params'])
        response = Response(200, {'Content-Type':fixture['content_type']}, fixture['listing_body'], url)
        items = ListingParser(p.listing['parser'], adapter=adapter).parse(response).items
        self.assertEqual(2, len(items))
        papers = []; calls = []
        def fetch(url):
            self.assertIn(url, fixture['details'], 'undeclared detail request')
            calls.append(url)
            value = fixture['details'][url]
            body = json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value
            return Response(200, {'Content-Type':'application/json' if isinstance(value, dict) else 'text/html'},
                            body, url+'?moved=1' if redirect else url)
        for item in items:
            paper = adapter.materialize(item, SimpleNamespace(get=fetch))
            if paper is not None:
                papers.append(paper)
        self.assertEqual(len(fixture['details']), len(calls))
        return papers

    def test_full_paper_and_identity_equal_original_crawl(self):
        expected, _ = self.native()
        self.assertEqual(2, len(expected), 'fixture must exercise two actual saved papers')
        actual = self.pipeline()
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])
        self.assertEqual(2, len({paper_identity(p) for p in actual}))

    def test_short_abstract_skip_is_preserved(self):
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        expected, _ = self.native(f)
        self.assertEqual([], expected)
        self.assertEqual([], self.pipeline(f))

    def test_detail_redirect_preserves_registered_identity(self):
        expected, _ = self.native()
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(redirect=True))

    def test_caller_input_is_not_mutated(self):
        before = copy.deepcopy(self.fixture)
        self.pipeline()
        self.assertEqual(before, self.fixture)

    def test_all_original_methods_are_unchanged(self):
        code = (ROOT/'crawler/sites/custom'/(self.SITE+'.py')).read_text()
        tree = ast.parse(code)
        cls = next(x for x in tree.body if isinstance(x, ast.ClassDef))
        actual = {n.name:hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fixture['original_method_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)

    def test_added_listing_rejects_wrong_shape(self):
        c, _ = load(self.SITE)
        if self.SITE=='health-ni-gov-uk-publications':
            # Only Paper conversion is new here; never invent a URL/ID from missing fields.
            with self.assertRaises((KeyError, ValueError, TypeError)):
                with guarded(): c._enumeration_paper({}, {})
        else:
            args = [{'error':'not a listing'}]
            if self.SITE=='minfin-gov-gr-grafeio-typou': args.append(1)
            if self.SITE in ['home-cern-news','brgm-fr-fr']: args=['<html><body>error</body></html>']
            with self.assertRaises((ValueError, TypeError)):
                with guarded(): c._enumeration_items(*args)
