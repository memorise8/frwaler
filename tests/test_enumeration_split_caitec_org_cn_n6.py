"""caitec-org-cn-n6: path-paged static list JSON (ext:path_paging) with pure enumeration
methods. crawl() (unchanged) and the shipped plan are compared on synthetic data; no network."""
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.strategies import Strategy
from tests.collector_split_helpers import load, split_fixture, split_plan
from tests.collector_split2_helpers import Split2Checks

SITE = 'caitec-org-cn-n6'
FIRST = 'https://www.caitec.org.cn/n6/sy_xsyj_yjbg/json/index.html'


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = SITE

    def test_page_urls_are_the_crawl_urls(self):
        """Page 1 is json/index.html, page N is json/index_N.html (crawl()'s _fetch_list_page)."""
        from types import SimpleNamespace
        from crawler.enumeration.plan import site_plan
        p = site_plan(self.plan, SITE, registered=[SITE])
        strategy = Strategy(SimpleNamespace(plan=p))
        self.assertEqual(FIRST, strategy.page_url(1))
        self.assertEqual('https://www.caitec.org.cn/n6/sy_xsyj_yjbg/json/index_2.html', strategy.page_url(2))
        self.assertEqual('https://www.caitec.org.cn/n6/sy_xsyj_yjbg/json/index_7.html', strategy.page_url(7))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        """The list JSON is served as text/html: an empty data list is an explicit empty page,
        a non-JSON body is an invalid_json parse error (raised by fetch_page), never an end."""
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['empty_body'], FIRST))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', FIRST))
        self.assertEqual('invalid_json', error.extra.get('parse_error'))
        self.assertFalse(error.extra.get('explicit_empty', False))
        with self.assertRaises(PermanentFailure) as ctx:
            parser.parse(Response(200, {'Content-Type': 'text/html'}, '{"error":"maintenance"}', FIRST))
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_html_error_page_is_a_listing_error(self):
        crawler, _ = load(SITE)
        with self.assertRaises(PermanentFailure) as ctx:
            crawler._enumeration_items(['not', 'a', 'listing'])
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))


def run_path(fixture=None, first_body=None, first_type=None):
    """The shipped plan through the real SiteRunner: index.html serves the fixture listing,
    every index_N.html is 404 (static file past the last page)."""
    from tests.enumeration_fakes import Env
    fixture = fixture or split_fixture(SITE)
    plan = copy.deepcopy(split_plan(SITE))
    plan['request_interval_s'] = 0
    requested = []

    def handler(url):
        requested.append(url)
        if url.endswith('/robots.txt'):
            return Response(404, {}, b'', url)
        if url in fixture['details']:
            return Response(200, {'Content-Type': 'text/html'}, fixture['details'][url], url)
        if url == FIRST:
            return Response(200, {'Content-Type': first_type or fixture['content_type']},
                            fixture['listing_body'] if first_body is None else first_body, url)
        return Response(404, {'Content-Type': 'text/html'}, '<html>404</html>', url)
    env = Env(SITE)
    try:
        _, module = load(SITE)
        cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
        crawler = cls(env.conn, 0)
        with contextlib.redirect_stdout(io.StringIO()):
            result = env.run(plan, handler, crawler=crawler)
        return result, env.docs(), requested
    finally:
        env.close()


class RegisteredSplitRun(unittest.TestCase):
    def setUp(self):
        self.fixture = split_fixture(SITE)
        self.denials = []

        def denied(*args, **kwargs):
            self.denials.append('attempted I/O')
            raise AssertionError('split run attempted network/process I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'socket.socket.connect_ex',
                     'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)
        self.addCleanup(lambda: self.assertEqual([], self.denials))

    def test_shipped_plan_completes_end_to_end(self):
        result, docs, requested = run_path()
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual({'9001', '9002'}, {d['post_number'] for d in docs})
        self.assertEqual(2, len({d['meta_url'] for d in docs}))
        self.assertIn('https://www.caitec.org.cn/n6/sy_xsyj_yjbg/json/index_2.html', requested)
        self.assertFalse([u for u in requested if 'page=' in u or u.endswith('/index_1.html')])

    def test_error_listing_ends_incomplete_as_listing_parse_error(self):
        cases = {'maintenance_page': ('<html><body><p>Site under maintenance</p></body></html>', 'text/html'),
                 'json_without_data': (json.dumps({'error': 'x'}), 'text/html')}
        for name, (body, content_type) in cases.items():
            with self.subTest(case=name):
                result, docs, _ = run_path(first_body=body, first_type=content_type)
                self.assertEqual('incomplete', result['status'], result)
                self.assertTrue(result['stop_reason'].startswith('listing_failed:listing_parse_error:'), result)
                self.assertEqual([], docs)

    def test_short_paper_does_not_silently_become_complete(self):
        fixture = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        result, docs, _ = run_path(fixture=fixture)
        self.assertEqual('complete_with_review', result['status'], result)
        self.assertEqual([], docs)
