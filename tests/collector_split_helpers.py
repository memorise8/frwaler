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
from urllib.parse import parse_qs, urlencode, urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from crawler.enumeration.urls import build_url

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


WP_PAST_LAST_PAGE = json.dumps({'code': 'rest_post_invalid_page_number',
                                'message': 'The page number requested is larger than the number of pages available.',
                                'data': {'status': 400}})


def split_plan(site):
    return json.loads((ROOT/'docs/examples/collector-splits'/(site+'.json')).read_text())['sites'][site]


def split_fixture(site):
    return json.loads((ROOT/'tests/fixtures/collector_splits'/(site+'.json')).read_text())


def wp_past_last_page(url=''):
    """WordPress REST core's answer for a page number past the last page."""
    return Response(400, {'Content-Type': 'application/json'}, WP_PAST_LAST_PAGE, url)


def run_split(site, *, end=None, listing=None, listing_type=None, total=None, plan=None, fixture=None):
    """Synthetic end-to-end run of a shipped plan through the real SiteRunner.run.
    Fake transport only: the start page (or the page-less URL) serves the fixture
    listing, every later page serves ``end(url)`` (default: WordPress past-last-page 400).
    Returns (runner result, saved documents)."""
    from tests.enumeration_fakes import Env
    fixture = fixture or split_fixture(site)
    plan = copy.deepcopy(plan or split_plan(site))
    # Pacing only (not part of the plan fingerprint): the fake source needs no real wait.
    plan['request_interval_s'] = 0
    page_param = plan['listing'].get('page_param', 'page')
    start = plan['listing'].get('start_page', 1)
    end = end or wp_past_last_page

    def handler(url):
        parts = urlsplit(url)
        if parts.path == '/robots.txt':
            return Response(404, {}, b'')
        if url in fixture['details']:
            value = fixture['details'][url]
            body = json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value
            return Response(200, {'Content-Type': 'application/json' if isinstance(value, dict) else 'text/html'},
                            body, url)
        query = parse_qs(parts.query)
        page = int(query[page_param][0]) if page_param in query else start
        if page <= start:
            headers = {'Content-Type': listing_type or fixture['content_type']}
            if total is not None:
                headers['X-WP-Total'] = str(total)
            return Response(200, headers, fixture['listing_body'] if listing is None else listing, url)
        return end(url)

    env = Env(site)
    try:
        _, module = load(site)
        cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == site)
        try:
            crawler = cls(env.conn, 0)
        except TypeError:
            crawler = cls(env.conn)
        with contextlib.redirect_stdout(io.StringIO()):
            result = env.run(plan, handler, crawler=crawler)
        return result, env.docs()
    finally:
        env.close()


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
        self.raw_papers = []
        method_name = '_build_paper' if self.SITE=='brgm-fr-fr' else '_enumeration_paper'
        original = getattr(crawler, method_name)
        def capture_conversion(*args, **kwargs):
            result = original(*args, **kwargs)
            if result is not None:
                self.raw_papers.append(copy.deepcopy(result))
            return result
        setattr(crawler, method_name, capture_conversion)
        url = p.listing['url']
        if p.listing.get('params'):
            url += ('&' if '?' in url else '?') + urlencode(p.listing['params'])
        response = Response(200, {'Content-Type':fixture['content_type']}, fixture['listing_body'], url)
        items = ListingParser(p.listing['parser'], adapter=adapter).parse(response).items
        self.assertEqual(2, len(items))
        if p.strategy == 'last_page_backward' and p.section('last_page').get('check_unparameterized_first_page', True):
            # The strategy first compares the page-less listing URL with the start page;
            # list bindings must work on that URL too (or the plan must turn the check off).
            bare = build_url(p.listing['url'], p.listing.get('params') or {}, drop={p.listing.get('page_param', 'page')})
            bare_items = ListingParser(p.listing['parser'], adapter=adapter).parse(
                Response(200, {'Content-Type':fixture['content_type']}, fixture['listing_body'], bare)).items
            self.assertEqual([i.key for i in items], [i.key for i in bare_items])
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
        self.assertEqual(expected, self.raw_papers, 'raw fields must match before adapter cleaning')
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
            return
        args = [{'error':'not a listing'}]
        if self.SITE=='minfin-gov-gr-grafeio-typou': args.append(1)
        if self.SITE in ['home-cern-news','brgm-fr-fr']: args=['<html><body>error</body></html>']
        try:
            with guarded(): rows = c._enumeration_items(*args)
        except PermanentFailure as exc:
            # Classified by the runner as a listing parse error (incomplete), never runner_error.
            self.assertTrue(exc.reason.startswith('listing_parse_error:'), exc.reason)
        else:
            # An empty result is judged by the plan's listing_marker (see the run tests below).
            self.assertEqual([], rows)
            self.assertTrue(self.plan['sites'][self.SITE]['listing']['parser'].get('listing_marker'))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        adapter = Adapter(self.SITE, p.section('adapter'), None, crawler=crawler)
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        url = p.listing['url'] + ('&' if '?' in p.listing['url'] else '?') + urlencode(p.listing.get('params', {}))
        content_type = self.fixture['content_type']
        empty = ('[]' if content_type=='application/json' else
                 '<rss><channel/></rss>' if 'rss' in content_type else '<html><main/></html>')
        page = parser.parse(Response(200, {'Content-Type':content_type}, empty, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        self.assertNotIn('parse_error', page.extra)
        try:
            error_page = parser.parse(Response(200, {'Content-Type':'text/html'}, '<html>maintenance</html>', url))
        except PermanentFailure as exc:
            self.assertTrue(exc.reason.startswith('listing_parse_error:'), exc.reason)
        else:
            self.assertTrue(error_page.extra.get('parse_error'))
            self.assertFalse(error_page.extra.get('explicit_empty', False))


class SplitRunChecks:
    """Synthetic end-to-end runs of the shipped plan through the real SiteRunner.run.
    The temporary state/documents SQLite of tests.enumeration_fakes.Env is used;
    network and subprocess calls fail the test."""

    def setUp(self):
        self.fixture = split_fixture(self.SITE)
        self.denials = []
        def denied(*args, **kwargs):
            self.denials.append('attempted I/O')
            raise AssertionError('split run attempted network/process I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'socket.socket.connect_ex',
                     'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)
        self.addCleanup(lambda: self.assertEqual([], self.denials))

    def test_error_listing_ends_incomplete_as_listing_parse_error(self):
        """Maintenance page, a 200 JSON error object and a blank line before the XML
        declaration end the real runner as incomplete listing_parse_error, never runner_error."""
        cases = {'maintenance_page': ('<html><body><p>Site under maintenance</p></body></html>', 'text/html')}
        if self.fixture['content_type'] == 'application/json':
            cases['json_error_object'] = (json.dumps({'code': 'rest_no_route', 'message': 'No route',
                                                      'data': {'status': 404}}), 'application/json')
        if 'rss' in self.fixture['content_type']:
            cases['blank_line_before_xml_declaration'] = (
                '\n<?xml version="1.0" encoding="UTF-8"?>' + self.fixture['listing_body'], self.fixture['content_type'])
        for name, (body, content_type) in cases.items():
            with self.subTest(case=name):
                result, docs = run_split(self.SITE, listing=body, listing_type=content_type)
                self.assertEqual('incomplete', result['status'], result)
                self.assertTrue(result['stop_reason'].startswith('listing_failed:listing_parse_error:'), result)
                self.assertEqual([], docs)
