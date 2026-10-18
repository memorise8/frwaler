"""sastind-gov-cn-n10086200: path-paged static list (ext:path_paging): index.html is the newest
page, index_10126608_N.html count down to N=1 (the oldest page). Pure enumeration methods;
crawl() (unchanged) and the shipped plan are compared on synthetic data; no network."""
import contextlib
import copy
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.plan import site_plan
from crawler.enumeration.strategies import Strategy
from tests.collector_split_helpers import load, split_fixture, split_plan
from tests.collector_split2_helpers import Split2Checks

SITE = 'sastind-gov-cn-n10086200'
FIRST = 'https://www.sastind.gov.cn/n10086200/n10086319/index.html'


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        """crawl() with a memory transport (detail_delay is set by __init__, which the
        loader does not run)."""
        fixture = fixture or self.fixture
        crawler, _ = load(SITE)
        captured, calls, listing_calls = [], [], []

        def transport(url, *args, **kwargs):
            calls.append(url)
            if url in fixture['details']:
                return fixture['details'][url]
            listing_calls.append(url)
            return fixture['listing_body'] if len(listing_calls) == 1 else fixture['empty_body']
        crawler._delay = 0
        crawler.detail_delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_get = transport
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertEqual([FIRST], listing_calls)
        return captured, calls

    def test_page_urls(self):
        """Page 0 is index.html (crawl()'s first page); page N is index_10126608_N.html. N counts
        from the oldest page (N=1), which crawl()'s page loop never requests."""
        crawler, _ = load(SITE)
        strategy = Strategy(SimpleNamespace(plan=site_plan(self.plan, SITE, registered=[SITE])))
        self.assertEqual(crawler._list_url(1), strategy.page_url(0))
        self.assertEqual('https://www.sastind.gov.cn/n10086200/n10086319/index_10126608_1.html', strategy.page_url(1))
        for n in (2, 3, 393):
            self.assertEqual(crawler._list_url(n), strategy.page_url(n))

    def test_page_without_list_block_is_a_listing_error(self):
        crawler, _ = load(SITE)
        with self.assertRaises(PermanentFailure) as ctx:
            crawler._enumeration_items('<html><body><ul><li><a href="/c1/content.html">x</a></li></ul></body></html>')
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))


def run_path(fixture=None, first_body=None):
    """The shipped plan through the real SiteRunner: index.html serves the fixture listing,
    every index_10126608_N.html is 404 (static file past the last page)."""
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
            return Response(200, {'Content-Type': 'text/html'},
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
        self.assertEqual({'90000001', '90000002'}, {d['post_number'] for d in docs})
        self.assertEqual(2, len({d['meta_url'] for d in docs}))
        self.assertIn('https://www.sastind.gov.cn/n10086200/n10086319/index_10126608_1.html', requested)
        self.assertFalse([u for u in requested if 'page=' in u or u.endswith('_0.html')])

    def test_error_listing_ends_incomplete_as_listing_parse_error(self):
        result, docs, _ = run_path(first_body='<html><body><p>Site under maintenance</p></body></html>')
        self.assertEqual('incomplete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('listing_failed:listing_parse_error:'), result)
        self.assertEqual([], docs)

    def test_short_paper_does_not_silently_become_complete(self):
        fixture = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        result, docs, _ = run_path(fixture=fixture)
        self.assertEqual('complete_with_review', result['status'], result)
        self.assertEqual([], docs)
