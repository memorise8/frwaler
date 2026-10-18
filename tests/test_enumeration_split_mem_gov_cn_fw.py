"""mem-gov-cn-fw: path-paged TRS list (ext:path_paging, root page then index_N.shtml) with
pure enumeration methods. crawl() (unchanged) and the shipped plan are compared on synthetic
data; no network."""
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

SITE = 'mem-gov-cn-fw'
FIRST = 'https://www.mem.gov.cn/fw/yajzjxzxk/zjxzxksxbljg/'


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = SITE

    def test_page_urls_are_the_crawl_urls(self):
        """Page index 0 is the channel root, index i is index_i.shtml (crawl()'s _list_url)."""
        crawler, _ = load(SITE)
        strategy = Strategy(SimpleNamespace(plan=site_plan(self.plan, SITE, registered=[SITE])))
        for i in range(0, 6):
            self.assertEqual(crawler._list_url(i), strategy.page_url(i))

    def test_page_without_trs_pager_is_a_listing_error(self):
        crawler, _ = load(SITE)
        with self.assertRaises(PermanentFailure) as ctx:
            crawler._enumeration_items('<html><body><ul><li>maintenance</li></ul></body></html>')
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))


def run_path(fixture=None, first_body=None):
    """The shipped plan through the real SiteRunner: the root serves the fixture listing,
    every index_N.shtml is 404 (static file past the last page)."""
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
        self.assertEqual({'t20261101_900001', 't20261202_900002'}, {d['post_number'] for d in docs})
        self.assertEqual(2, len({d['meta_url'] for d in docs}))
        self.assertIn(FIRST + 'index_1.shtml', requested)
        self.assertFalse([u for u in requested if 'page=' in u or u.endswith('/index_0.shtml')])

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
