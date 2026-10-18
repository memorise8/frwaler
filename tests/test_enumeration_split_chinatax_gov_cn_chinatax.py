"""chinatax-gov-cn-chinatax: POST listing (ext:post_listing) with pure enumeration methods.
crawl() (unchanged) and the shipped plan are compared on synthetic data; no network."""
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from crawler.enumeration.http import Response
from tests.collector_split_helpers import load, split_fixture, split_plan
from tests.collector_split2_helpers import Split2Checks

SITE = 'chinatax-gov-cn-chinatax'
LIST_URL = 'https://www.chinatax.gov.cn/getFileListByCodeId'


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        """crawl() with memory transports: the listing is its POST form helper."""
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        captured, calls, posts = [], [], []

        def post(url, data):
            posts.append((url, dict(data)))
            return fixture['listing_body'] if len(posts) == 1 else fixture['empty_body']

        def get(url):
            calls.append(url)
            return fixture['details'][url]
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_post_form = post
        crawler._curl_get = get
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertEqual(LIST_URL, posts[0][0])
        self.assertEqual(1, posts[0][1]['page'])
        return captured, calls

    def test_plan_posts_the_same_form_as_crawl(self):
        listing = self.plan['sites'][SITE]['listing']
        self.assertEqual('POST', listing['method'])
        self.assertTrue(listing['idempotent'])
        self.assertEqual('form', listing['body_type'])
        self.assertEqual({'channelId': '61eaab3127b040d4950fff177f09e561', 'size': 20, 'relateSubChannels': 'true'},
                         listing['params'])
        self.assertEqual('page', listing['page_param'])


class SyntheticSource:
    """POST-aware transport: robots 404, listing form POSTs, detail GETs."""

    def __init__(self, fixture, listing=None, listing_type='application/json'):
        self.fixture, self.listing, self.listing_type = fixture, listing, listing_type
        self.calls = []

    def request(self, method, url, *, headers=None, data=None):
        self.calls.append((method, url, data))
        parts = urlsplit(url)
        if parts.path == '/robots.txt':
            return Response(404, {}, b'', url)
        if url in self.fixture['details']:
            return Response(200, {'Content-Type': 'text/html'}, self.fixture['details'][url], url)
        if url == LIST_URL:
            if method != 'POST':
                return Response(404, {'Content-Type': 'text/html'}, '内部服务器错误', url)
            body = parse_qs(data.decode() if isinstance(data, bytes) else data)
            page = int(body.get('page', ['1'])[0])
            if page == 1:
                return Response(200, {'Content-Type': self.listing_type},
                                self.fixture['listing_body'] if self.listing is None else self.listing, url)
            return Response(200, {'Content-Type': 'application/json'}, self.fixture['empty_body'], url)
        return Response(404, {}, b'', url)


def run_post(fixture=None, listing=None, listing_type='application/json'):
    from tests.enumeration_fakes import Env
    fixture = fixture or split_fixture(SITE)
    plan = copy.deepcopy(split_plan(SITE))
    plan['request_interval_s'] = 0
    source = SyntheticSource(fixture, listing, listing_type)
    env = Env(SITE)
    try:
        _, module = load(SITE)
        cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
        crawler = cls(env.conn, 0)
        with contextlib.redirect_stdout(io.StringIO()):
            result = env.run(plan, None, crawler=crawler, transport=source)
        return result, env.docs(), source
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
        result, docs, source = run_post()
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual({'9000001', '9000002'}, {d['post_number'] for d in docs})
        self.assertEqual(2, len({d['meta_url'] for d in docs}))
        self.assertEqual('/robots.txt', urlsplit(source.calls[0][1]).path)  # robots before any POST
        posts = [c for c in source.calls if c[1] == LIST_URL]
        self.assertTrue(posts and all(m == 'POST' for m, _, _ in posts))

    def test_error_listing_ends_incomplete_as_listing_parse_error(self):
        cases = {'maintenance_page': ('<html><body><p>Site under maintenance</p></body></html>', 'text/html'),
                 'json_error_object': (json.dumps({'code': 500, 'msg': 'error'}), 'application/json')}
        for name, (body, content_type) in cases.items():
            with self.subTest(case=name):
                result, docs, _ = run_post(listing=body, listing_type=content_type)
                self.assertEqual('incomplete', result['status'], result)
                self.assertTrue(result['stop_reason'].startswith('listing_failed:listing_parse_error:'), result)
                self.assertEqual([], docs)

    def test_short_paper_does_not_silently_become_complete(self):
        fixture = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        result, docs, _ = run_post(fixture=fixture)
        self.assertEqual('complete_with_review', result['status'], result)
        self.assertEqual([], docs)
