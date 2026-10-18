"""english-cnipa-gov-cn-col: registered scope as 27 fixed explicit segments + pure conversion.

The registered crawl() reads the main page /col/col1336/index.html, takes the year
sub-collections from div.column_list (27 years, 1999..2025, checked 2026-10-18) and, per
year, reads the jpage dataproxy listing (columnid + unitid from the year page) or, for a
year page without a unitid (2021 col2936, 2022 col3261), the download links of the year
page itself. The shipped plan fixes those 27 years as explicit date_range_split segments
substituted into ``listing.url`` ({segment}); the year label comes from the collector's
fixed column table. A column outside the table is a listing error, so a new report year
shows up as a plan + table update instead of being skipped silently.
Memory only: crawl() (unchanged) and the shipped plan are compared on synthetic data.
"""
import ast
import contextlib
import copy
import hashlib
import io
import json
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from crawler.enumeration.strategies import DateRangeSplit, generate_segments
from tests.collector_split_helpers import ROOT, load, split_fixture, split_plan

SITE = 'english-cnipa-gov-cn-col'
BASE = 'https://english.cnipa.gov.cn'
PROXY_KEY = 'module/web/jpage/dataproxy.jsp?columnid=3698'
PAGE_KEY = 'col/col2936/index.html'
PAGE_COLUMNS = {'2936', '3261'}


def _deny_io(case):
    case.denials = []

    def denied(*args, **kwargs):
        case.denials.append('attempted I/O')
        raise AssertionError('split test attempted network/process/database I/O')
    for name in ('socket.getaddrinfo', 'socket.socket.connect', 'socket.socket.connect_ex',
                 'subprocess.Popen._execute_child'):
        p = patch(name, denied); p.start(); case.addCleanup(p.stop)
    case.addCleanup(lambda: case.assertEqual([], case.denials))


def _strategy(plan):
    p = site_plan(plan, SITE, registered=[SITE])
    return p, DateRangeSplit(SimpleNamespace(plan=p, today=date(2026, 10, 18)))


def _column_of(url):
    parts = urlsplit(url)
    if parts.path == '/module/web/jpage/dataproxy.jsp':
        return 'proxy', parse_qs(parts.query)['columnid'][0], int(parse_qs(parts.query).get('page', ['1'])[0])
    if parts.path.startswith('/col/col') and parts.path.endswith('/index.html'):
        return 'page', parts.path[len('/col/col'):-len('/index.html')], None
    return None, None, None


class RegisteredSplit(unittest.TestCase):
    def setUp(self):
        self.fixture = split_fixture(SITE)
        self.plan = json.loads((ROOT/'docs/examples/collector-splits'/(SITE+'.json')).read_text())
        _deny_io(self)

    # -- the registered crawl() on the synthetic site ---------------------------------------
    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, module = load(SITE)
        captured, calls = [], []

        def transport(url, *args, **kwargs):
            calls.append(url)
            if url == module._START_URL:
                return fixture['hub_body']
            if url in fixture['details']:
                return fixture['details'][url]
            kind, col, page = _column_of(url)
            if kind == 'page':
                return fixture['column_bodies'][col]
            if kind == 'proxy':
                return fixture['listing_body'] if (col, page) == ('3698', 1) else fixture['empty_body']
            raise AssertionError('unexpected request ' + url)
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._check_cancelled = lambda: None
        crawler._report_source_failure = lambda *a, **k: None
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch.object(module, '_curl_get', transport), patch('time.sleep', lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        return captured, calls

    # -- the shipped plan's conversion path ---------------------------------------------------
    def pipeline(self, fixture=None, redirect=False):
        fixture = fixture or self.fixture
        p, strat = _strategy(self.plan)
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        self.raw_papers = []
        original = crawler._enumeration_paper

        def capture(*args, **kwargs):
            result = original(*args, **kwargs)
            if result is not None:
                self.raw_papers.append(copy.deepcopy(result))
            return result
        crawler._enumeration_paper = capture
        parser = ListingParser(p.listing['parser'], total_cfg=p.listing.get('total'), adapter=adapter)
        items = []
        bodies = {}
        for key, body in ((PROXY_KEY, fixture['listing_body']), (PAGE_KEY, fixture['column_bodies']['2936'])):
            url = strat.page_url(1, extra=strat.filter_params(key), url=strat.segment_url(key))
            bodies[url] = body
            items.extend(parser.parse(Response(200, {'Content-Type': 'text/html'}, body, url)).items)
        self.assertEqual(2, len(items))
        papers, calls = [], []

        def fetch(url):
            calls.append(url)
            body = fixture['details'].get(url, bodies.get(url))
            self.assertIsNotNone(body, 'undeclared detail request ' + url)
            return Response(200, {'Content-Type': 'text/html'}, body, url + '#moved' if redirect else url)
        for item in items:
            paper = adapter.materialize(item, SimpleNamespace(get=fetch))
            if paper is not None:
                papers.append(paper)
        self.assertEqual(2, len(calls))
        return papers

    @staticmethod
    def by_id(papers):
        return sorted(papers, key=lambda x: x['external_id'])

    def test_full_paper_and_identity_equal_original_crawl(self):
        expected, _ = self.native()
        self.assertEqual(2, len(expected), 'fixture must exercise a dataproxy and a column-page paper')
        actual = self.pipeline()
        self.assertEqual(self.by_id(expected), self.by_id(self.raw_papers), 'raw fields must match')
        self.assertEqual(self.by_id([clean_paper(p) for p in expected]), self.by_id(actual))
        self.assertEqual(sorted(paper_identity(p) for p in expected), sorted(paper_identity(p) for p in actual))
        self.assertEqual({('col3698_90000001', BASE + '/art/2026/8/4/art_3698_90000001.html'),
                          ('col2936_90000002', BASE + '/module/download/down.jsp?i_ID=90000002&colID=2936')},
                         {paper_identity(p) for p in actual})
        years = {json.loads(p['metadata'])['year'] for p in actual}
        self.assertEqual({'2025', '2021'}, years)

    def test_detail_redirect_preserves_registered_identity(self):
        expected, _ = self.native()
        self.assertEqual(self.by_id([clean_paper(p) for p in expected]), self.by_id(self.pipeline(redirect=True)))

    def test_caller_input_is_not_mutated(self):
        before = copy.deepcopy(self.fixture)
        self.pipeline()
        self.assertEqual(before, self.fixture)

    def test_all_original_methods_are_unchanged(self):
        code = (ROOT/'crawler/sites/custom'/(SITE+'.py')).read_text()
        tree = ast.parse(code)
        cls = next(x for x in tree.body if isinstance(x, ast.ClassDef))
        methods = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                   for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fixture['original_method_source_sha256'].items():
            self.assertEqual(digest, methods[name], name)
        functions = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                     for n in tree.body if isinstance(n, ast.FunctionDef)}
        self.assertEqual(self.fixture['module_function_source_sha256'], functions)

    def test_segments_are_the_fixed_year_table(self):
        """27 explicit segments, oldest first, one per column of the fixed table; the two
        unitid-less years read their column page, every other year the dataproxy listing."""
        crawler, _ = load(SITE)
        p, strat = _strategy(self.plan)
        keys = generate_segments(strat.cfg, None)
        table = crawler._ENUM_COLUMN_YEARS
        self.assertEqual(27, len(keys))
        self.assertEqual([str(y) for y in range(1999, 2026)], sorted(table.values()))
        cols = []
        for key in keys:
            url = strat.page_url(1, extra=strat.filter_params(key), url=strat.segment_url(key))
            kind, col, _ = _column_of(url)
            cols.append(col)
            self.assertEqual(kind == 'page', col in PAGE_COLUMNS, key)
            if kind == 'proxy':
                q = parse_qs(urlsplit(url).query)
                self.assertEqual({'webid': ['2'], 'path': [BASE + '/'], 'columnid': [col], 'unitid': ['14819'],
                                  'webname': ['China National Intellectual Property Administration'],
                                  'permissiontype': ['0'], 'page': ['1']}, q)
        self.assertEqual(set(table), set(cols))
        self.assertEqual(PAGE_COLUMNS, set(crawler._ENUM_PAGE_COLUMNS))
        self.assertEqual(sorted(table.values()), [table[c] for c in cols])  # oldest first

    def test_unknown_or_mismatched_column_is_a_listing_error(self):
        crawler, _ = load(SITE)
        proxy = BASE + '/module/web/jpage/dataproxy.jsp?columnid={}&unitid=14819&page=1'
        cases = [
            (self.fixture['listing_body'], proxy.format('3799')),          # a new year column
            (self.fixture['listing_body'], BASE + '/module/web/jpage/dataproxy.jsp?page=1'),
            (self.fixture['listing_body'], proxy.format('2936')),          # page column via dataproxy
            (self.fixture['column_bodies']['2936'], BASE + '/col/col3698/index.html?page=1'),
            (self.fixture['column_bodies']['3698'], BASE + '/col/col2936/index.html?page=1'),  # now has unitid
            ('<html><body></body></html>', BASE + '/col/col2936/index.html?page=1'),
            ('<html><body>maintenance</body></html>', proxy.format('3698')),
            (self.fixture['listing_body'], BASE + '/other/path.html'),
        ]
        for body, url in cases:
            with self.subTest(url=url, body=body[:30]):
                with self.assertRaises(PermanentFailure) as ctx:
                    with guarded():
                        crawler._enumeration_items(body, url)
                self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'), ctx.exception.reason)
        with self.assertRaises(PermanentFailure) as ctx:
            crawler._enumeration_items(self.fixture['listing_body'], proxy.format('3799'))
        self.assertEqual('listing_parse_error:unknown_column:3799', ctx.exception.reason)

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p, strat = _strategy(self.plan)
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], total_cfg=p.listing.get('total'),
                               adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        url = strat.page_url(2, extra=strat.filter_params(PROXY_KEY), url=strat.segment_url(PROXY_KEY))
        page = parser.parse(Response(200, {'Content-Type': 'text/xml'}, self.fixture['empty_body'], url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        with self.assertRaises(PermanentFailure) as ctx:
            parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))
        first = parser.parse(Response(200, {'Content-Type': 'text/xml'}, self.fixture['listing_body'],
                                      url.replace('page=2', 'page=1')))
        self.assertEqual(1, first.total)


def synthetic_site(fixture):
    """Every year of the fixed table: dataproxy columns answer one record on page 1 and an
    empty datastore after it; the two column-page years answer one download link for any ?page."""
    template = fixture['listing_body']
    detail = next(iter(fixture['details'].values()))

    def handler(url):
        parts = urlsplit(url)
        if parts.path == '/robots.txt':
            return Response(404, {}, b'')
        kind, col, page = _column_of(url)
        if parts.path.startswith('/art/'):
            art = parts.path.rsplit('_', 1)[-1][:-len('.html')]
            return Response(200, {'Content-Type': 'text/html'}, detail.replace('90000001', art), url)
        if kind == 'proxy':
            body = (template.replace('3698', col).replace('90000001', '9' + col.zfill(7))
                    if page == 1 else fixture['empty_body'])
            return Response(200, {'Content-Type': 'text/xml'}, body, url)
        if kind == 'page' and col in PAGE_COLUMNS:
            body = fixture['column_bodies']['2936'].replace('2936', col).replace('90000002', '8' + col.zfill(7))
            return Response(200, {'Content-Type': 'text/html'}, body, url)
        return Response(404, {'Content-Type': 'text/html'}, '<html>not found</html>', url)
    return handler


def run_plan(fixture, plan=None, handler=None):
    from tests.enumeration_fakes import Env
    plan = copy.deepcopy(plan or split_plan(SITE))
    plan['request_interval_s'] = 0
    env = Env(SITE)
    try:
        _, module = load(SITE)
        crawler = module.CnipaAnnualReportCrawler(env.conn)
        with contextlib.redirect_stdout(io.StringIO()):
            result = env.run(plan, handler or synthetic_site(fixture), crawler=crawler)
        return result, env.docs()
    finally:
        env.close()


class RegisteredSplitRun(unittest.TestCase):
    def setUp(self):
        self.fixture = split_fixture(SITE)
        _deny_io(self)

    def test_shipped_plan_completes_end_to_end(self):
        result, docs = run_plan(self.fixture)
        self.assertEqual('complete', result['status'], result)
        self.assertEqual('all_segments_reconciled', result['stop_reason'], result)
        self.assertEqual(27, len(docs))
        self.assertEqual(27, len({d['post_number'] for d in docs}))
        self.assertEqual(27, len({d['meta_url'] for d in docs}))
        self.assertIn('col1570_90001570', {d['post_number'] for d in docs})
        self.assertIn('col3261_80003261', {d['post_number'] for d in docs})

    def test_new_year_segment_without_table_entry_stops_the_listing(self):
        plan = copy.deepcopy(split_plan(SITE))
        extra = copy.deepcopy(plan['segments']['items'][-1])
        extra['key'] = 'module/web/jpage/dataproxy.jsp?columnid=3799'
        plan['segments']['items'].append(extra)
        result, _ = run_plan(self.fixture, plan=plan)
        self.assertEqual('incomplete', result['status'], result)
        self.assertIn('listing_parse_error:unknown_column:3799', result['stop_reason'])

    def test_error_listing_ends_incomplete_as_listing_parse_error(self):
        site = synthetic_site(self.fixture)

        def handler(url):
            if '/dataproxy.jsp' in url and 'columnid=1570' in url:
                return Response(200, {'Content-Type': 'text/html'}, '<html><body>Site under maintenance</body></html>', url)
            return site(url)
        result, docs = run_plan(self.fixture, handler=handler)
        self.assertEqual('incomplete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('listing_failed:listing_parse_error:'), result)


if __name__ == '__main__':
    unittest.main()
