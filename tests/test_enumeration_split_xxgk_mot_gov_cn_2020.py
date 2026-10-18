"""xxgk-mot-gov-cn-2020: path-paged static list (ext:path_paging, iframe_list_7232.html then
iframe_list_7232_N.html) with pure enumeration methods. crawl() (unchanged) and the shipped
plan are compared on synthetic data; no network."""
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

SITE = 'xxgk-mot-gov-cn-2020'
FIRST = 'https://xxgk.mot.gov.cn/2020/zhengce/863/868/869/iframe_list_7232.html'


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

    def pipeline(self, fixture=None, redirect=False):
        """The shared pipeline with the start page URL (the rows' crawl page number comes
        from the page URL)."""
        plan = copy.deepcopy(self.plan)
        listing = plan['sites'][SITE]['listing']
        listing = {k: v for k, v in listing.items() if k != 'first_page_url'}
        self.plan = {**plan, 'sites': {SITE: {**plan['sites'][SITE], 'listing': {**listing, 'url': FIRST}}}}
        try:
            return super().pipeline(fixture, redirect)
        finally:
            self.plan = plan

    def test_added_listing_rejects_wrong_shape(self):
        crawler, _ = load(SITE)
        with self.assertRaises(PermanentFailure) as ctx:
            crawler._enumeration_items({'error': 'not a listing'}, FIRST)
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.parsing import ListingParser
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['empty_body'], FIRST))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        with self.assertRaises(PermanentFailure) as ctx:
            parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', FIRST))
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_page_urls_are_the_crawl_urls(self):
        crawler, _ = load(SITE)
        strategy = Strategy(SimpleNamespace(plan=site_plan(self.plan, SITE, registered=[SITE])))
        for n in range(0, 5):
            self.assertEqual(crawler._list_url(n + 1), strategy.page_url(n))

    def test_rows_carry_the_crawl_page_number(self):
        crawler, _ = load(SITE)
        body = self.fixture['listing_body'].replace('createPageHTML(1, 0,', 'createPageHTML(5, 3,')
        rows = crawler._enumeration_items(body, FIRST.replace('7232.html', '7232_3.html'))
        self.assertEqual([4, 4], [r['list_page'] for r in rows])
        self.assertEqual([3, 3], [r['list_page_index'] for r in rows])

    def test_page_without_pager_or_foreign_url_is_a_listing_error(self):
        crawler, _ = load(SITE)
        for body, url in (('<html><ul><li><a href="/x.html">x</a></li></ul></html>', FIRST),
                          (self.fixture['listing_body'], 'https://xxgk.mot.gov.cn/')):
            with self.assertRaises(PermanentFailure) as ctx:
                crawler._enumeration_items(body, url)
            self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))


def run_path(fixture=None, first_body=None):
    """The shipped plan through the real SiteRunner: the start page serves the fixture
    listing, every iframe_list_7232_N.html is 404 (static file past the last page)."""
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
        self.assertEqual({'9000001', '9000002'}, {d['post_number'] for d in docs})
        self.assertEqual(2, len({d['meta_url'] for d in docs}))
        self.assertIn(FIRST.replace('7232.html', '7232_1.html'), requested)
        self.assertFalse([u for u in requested if 'page=' in u or u.endswith('7232_0.html')])

    def test_error_listing_ends_incomplete_as_listing_parse_error(self):
        result, docs, _ = run_path(first_body='<html><body><p>Site under maintenance</p></body></html>')
        self.assertEqual('incomplete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('listing_failed:listing_parse_error:'), result)
        self.assertEqual([], docs)

    def test_short_body_keeps_items_like_crawl(self):
        """crawl() builds the abstract from body, fields and attachment lines: a short body
        with an attachment still passes its threshold, so both keep the items."""
        fixture = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        result, docs, _ = run_path(fixture=fixture)
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(2, len(docs))

    def test_empty_detail_is_skipped_not_saved(self):
        fixture = copy.deepcopy(self.fixture)
        fixture['details'] = {u: '<html><body><div id="Zoom">x</div></body></html>' for u in fixture['details']}
        result, docs, _ = run_path(fixture=fixture)
        self.assertEqual('complete_with_review', result['status'], result)
        self.assertEqual([], docs)
