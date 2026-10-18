"""samr-gov-cn-zw: JPaas list API (page number inside the paramJson query value) + detail page
and the per-item article-unit API request (ext:detail_aux_request = second_step.url_template,
with ext:two_step_keep_first so both answers reach the paper method).

crawl() (unchanged) reads ``front/page/build/unit`` (pageType=column, paramJson
{"pageNo":N,"pageSize":20}), then per row the detail page and the same API with
pageType=article / tagId=文章正文2 / pageId=<row page_id> for metadata.related_links and a
fallback pdf_url. The shipped plan puts {page} in the encoded paramJson value (plain text
substitution of listing.url) and builds the unit request from the row's page_id.
Memory only: crawl() and the shipped plan are compared on synthetic data.
"""
import ast
import contextlib
import copy
import hashlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from crawler.enumeration.strategies import LastPageBackward
from tests.collector_split_helpers import ROOT, load, split_fixture, split_plan

SITE = 'samr-gov-cn-zw'
NEW_METHODS = {'_enumeration_items', '_enumeration_unit_html', '_enumeration_paper'}


def _deny_io(case):
    case.denials = []

    def denied(*args, **kwargs):
        case.denials.append('attempted I/O')
        raise AssertionError('split test attempted network/process/database I/O')
    for name in ('socket.getaddrinfo', 'socket.socket.connect', 'socket.socket.connect_ex',
                 'subprocess.Popen._execute_child'):
        p = patch(name, denied); p.start(); case.addCleanup(p.stop)
    case.addCleanup(lambda: case.assertEqual([], case.denials))


def _list_page(url):
    """Page number of a column-list request, None for any other URL."""
    parts = urlsplit(url)
    q = parse_qs(parts.query)
    if parts.path.endswith('/front/page/build/unit') and q.get('pageType') == ['column']:
        return json.loads(q['paramJson'][0])['pageNo']
    return None


def _body(value):
    return json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value


def _strategy(plan):
    p = site_plan(plan, SITE, registered=[SITE])
    return p, LastPageBackward(SimpleNamespace(plan=p))


class RegisteredSplit(unittest.TestCase):
    def setUp(self):
        self.fixture = split_fixture(SITE)
        self.plan = json.loads((ROOT/'docs/examples/collector-splits'/(SITE+'.json')).read_text())
        _deny_io(self)

    def native(self, fixture=None, redirect=False):
        fixture = fixture or self.fixture
        crawler, _ = load(SITE)
        captured, calls = [], []

        def transport(url, **kwargs):
            calls.append(url)
            page = _list_page(url)
            if page is not None:
                return (fixture['listing_body'] if page == 1 else fixture['empty_body']), url
            if url in fixture['details']:
                value = fixture['details'][url]
                # curl -L: crawl() uses the effective (post-redirect) detail URL
                return _body(value), (url + '?moved=1' if redirect and not isinstance(value, dict) else url)
            raise AssertionError('unexpected request ' + url)
        crawler.detail_delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_get_text = transport
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        return captured, calls

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
        url = strat.page_url(1)
        items = ListingParser(p.listing['parser'], adapter=adapter).parse(
            Response(200, {'Content-Type': 'application/json'}, fixture['listing_body'], url)).items
        self.assertEqual(2, len(items))
        papers, calls = [], []

        def fetch(u):
            self.assertIn(u, fixture['details'], 'undeclared detail request ' + u)
            calls.append(u)
            value = fixture['details'][u]
            final = u + '?moved=1' if redirect and not isinstance(value, dict) else u
            return Response(200, {'Content-Type': 'application/json' if isinstance(value, dict) else 'text/html'},
                            _body(value), final)
        for item in items:
            paper = adapter.materialize(item, SimpleNamespace(get=fetch))
            if paper is not None:
                papers.append(paper)
        self.assertEqual(len(fixture['details']), len(calls))
        return papers

    def test_listing_url_is_byte_identical_to_crawl(self):
        crawler, _ = load(SITE)
        _, strat = _strategy(self.plan)
        seen = []
        crawler._curl_get_text = lambda url, **kw: (seen.append(url), (None, url))[1]
        with contextlib.redirect_stdout(io.StringIO()):
            for n in (1, 2, 25):
                crawler._fetch_list_page(n)
        self.assertEqual(seen, [strat.page_url(n) for n in (1, 2, 25)])

    def test_unit_url_is_byte_identical_to_crawl(self):
        crawler, _ = load(SITE)
        p, _ = _strategy(self.plan)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        seen = []
        crawler._curl_get_text = lambda url, **kw: (seen.append(url), (None, url))[1]
        with contextlib.redirect_stdout(io.StringIO()):
            crawler._fetch_article_unit_html('aaaa1111', None)
        item = SimpleNamespace(payload={'page_id': 'aaaa1111'}, url='https://www.samr.gov.cn/x.html', key='k')
        self.assertEqual(seen, [adapter._step_link(Response(200, {}, b'', item.url), item)])

    def test_full_paper_and_identity_equal_original_crawl(self):
        expected, calls = self.native()
        self.assertEqual(2, len(expected))
        self.assertEqual(4, len([u for u in calls if u in self.fixture['details']]))  # detail + unit each
        actual = self.pipeline()
        self.assertEqual(expected, self.raw_papers, 'raw fields must match before adapter cleaning')
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])
        self.assertEqual(['00111', '00222'], [p['post_number'] for p in self.raw_papers])
        self.assertEqual(['aaaa1111', 'bbbb2222'], [p['external_id'] for p in self.raw_papers])
        meta = json.loads(actual[0]['metadata'])
        self.assertEqual('相关链接一', meta['related_links'][0]['title'])  # from the unit answer
        self.assertEqual(1, meta['list_page'])
        self.assertTrue(actual[0]['pdf_url'].endswith('/unit-a.pdf'))  # fallback from the unit answer
        self.assertTrue(actual[1]['pdf_url'].endswith('/doc-b.pdf'))  # detail page link wins

    def test_failed_unit_answer_matches_crawl(self):
        f = copy.deepcopy(self.fixture)
        for u, v in f['details'].items():
            if isinstance(v, dict):
                f['details'][u] = {'success': False, 'data': None}
        expected, _ = self.native(f)
        self.assertEqual([[], []], [json.loads(p['metadata'])['related_links'] for p in expected])
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(f))
        self.assertEqual(expected, self.raw_papers)

    def test_short_abstract_skip_is_preserved(self):
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], '短'))
        expected, _ = self.native(f)
        self.assertEqual([], expected)
        self.assertEqual([], self.pipeline(f))

    def test_detail_redirect_preserves_registered_identity(self):
        expected, _ = self.native(redirect=True)
        self.assertTrue(expected[0]['url'].endswith('?moved=1'))  # crawl() keeps the effective URL
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(redirect=True))
        self.assertEqual(['aaaa1111', 'bbbb2222'], [p['external_id'] for p in self.raw_papers])

    def test_caller_input_is_not_mutated(self):
        before = copy.deepcopy(self.fixture)
        self.pipeline()
        self.assertEqual(before, self.fixture)

    def test_network_hook_is_restored(self):
        crawler, _ = load(SITE)
        self.pipeline()
        self.assertNotIn('_fetch_article_unit_html', vars(crawler))

    def test_all_original_methods_are_unchanged(self):
        code = (ROOT/'crawler/sites/custom'/(SITE+'.py')).read_text()
        tree = ast.parse(code)
        cls = next(x for x in tree.body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef) and n.name not in NEW_METHODS}
        self.assertEqual(self.fixture['original_method_source_sha256'], actual)

    def test_added_listing_rejects_wrong_shape(self):
        crawler, _ = load(SITE)
        _, strat = _strategy(self.plan)
        url = strat.page_url(1)
        cases = [({'error': 'not a listing'}, url), ({'success': False, 'data': {}}, url), ([], url),
                 ({'success': True, 'data': {'html': '<div>maintenance</div>'}}, url),
                 ({'success': True, 'data': {'html': ''}}, url),
                 (json.loads(self.fixture['listing_body']), 'https://www.samr.gov.cn/other?x=1')]
        for payload, u in cases:
            with self.subTest(payload=str(payload)[:40], url=u):
                with self.assertRaises(PermanentFailure) as ctx:
                    with guarded():
                        crawler._enumeration_items(payload, u)
                self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'), ctx.exception.reason)

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p, strat = _strategy(self.plan)
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        url = strat.page_url(2)
        page = parser.parse(Response(200, {'Content-Type': 'application/json'}, self.fixture['empty_body'], url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        for body in ('{"success":false}', '<html>maintenance</html>'):
            with self.subTest(body=body):
                try:
                    error_page = parser.parse(Response(200, {'Content-Type': 'application/json'}, body, url))
                except PermanentFailure as exc:
                    self.assertTrue(exc.reason.startswith('listing_parse_error:'), exc.reason)
                else:
                    self.assertTrue(error_page.extra.get('parse_error'))
                    self.assertFalse(error_page.extra.get('explicit_empty', False))


def synthetic_site(fixture, listing=None):
    def handler(url):
        parts = urlsplit(url)
        if parts.path == '/robots.txt':
            return Response(404, {}, b'')
        page = _list_page(url)
        if page is not None:
            body = (listing if listing is not None else fixture['listing_body']) if page == 1 else fixture['empty_body']
            return Response(200, {'Content-Type': 'application/json'}, body, url)
        if url in fixture['details']:
            value = fixture['details'][url]
            return Response(200, {'Content-Type': 'application/json' if isinstance(value, dict) else 'text/html'},
                            _body(value), url)
        return Response(404, {'Content-Type': 'text/html'}, '<html>not found</html>', url)
    return handler


def run_plan(fixture, handler=None):
    from tests.enumeration_fakes import Env
    plan = copy.deepcopy(split_plan(SITE))
    plan['request_interval_s'] = 0
    env = Env(SITE)
    try:
        _, module = load(SITE)
        crawler = module.SamrGovCnZwCrawler(env.conn, 0)
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
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        # documents.post_number is the paper's external_id (page_id), as for crawl()
        self.assertEqual({'aaaa1111', 'bbbb2222'}, {d['post_number'] for d in docs})
        self.assertEqual(2, len({d['meta_url'] for d in docs}))

    def test_short_paper_does_not_silently_become_complete(self):
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], '短'))
        result, docs = run_plan(f)
        self.assertEqual('complete_with_review', result['status'], result)
        self.assertEqual([], docs)

    def test_error_listing_ends_incomplete_as_listing_parse_error(self):
        for name, body in (('json_error', '{"success":false,"msg":"error"}'),
                           ('maintenance', '{"success":true,"data":{"html":"<div>maintenance</div>"}}')):
            with self.subTest(case=name):
                result, docs = run_plan(self.fixture, handler=synthetic_site(self.fixture, listing=body))
                self.assertEqual('incomplete', result['status'], result)
                self.assertTrue(result['stop_reason'].startswith('listing_failed:listing_parse_error:'), result)
                self.assertEqual([], docs)


if __name__ == '__main__':
    unittest.main()
