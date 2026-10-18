"""inrae-fr-actualites: Algolia JSON POST listing (ext:post_listing, ext:headers) with pure
enumeration methods. crawl() (unchanged) and the shipped plan are compared on synthetic
data; no network. The search key is a fake test value; the plan names only its variable."""
import contextlib
import copy
import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from crawler.enumeration.adapters import clean_paper, paper_identity
from crawler.enumeration.http import Response
from crawler.enumeration.plan import header_hosts, resolve_headers, site_plan
from tests.collector_split_helpers import load, split_fixture, split_plan
from tests.collector_split2_helpers import Split2Checks

SITE = 'inrae-fr-actualites'
LIST_URL = 'https://dvutvwxjfu-dsn.algolia.net/1/indexes/inrae_prod_created_date_desc/query'
LIST_HOST = 'dvutvwxjfu-dsn.algolia.net'
KEY_ENV = 'INRAE_FR_ACTUALITES_KEY'
FAKE_KEY = 'fake-search-key-for-tests'


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        """crawl() with memory transports: the listing is its Algolia POST helper."""
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        captured, calls, posts = [], [], []

        def post(url, data, headers=None, context='POST'):
            posts.append((url, json.loads(data), dict(headers or {})))
            return fixture['listing_body'] if len(posts) == 1 else fixture['empty_body']

        def get(url, context='GET', referer=None):
            calls.append(url)
            return fixture['details'][url]
        crawler.detail_delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_post = post
        crawler._curl_get = get
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.posts = posts
        return captured, calls

    def test_plan_posts_the_same_query_as_crawl(self):
        self.native()
        url, body, headers = self.posts[0]
        site = self.plan['sites'][SITE]
        listing = site['listing']
        self.assertEqual(url.lower(), listing['url'])
        self.assertEqual(('POST', True, 'json'), (listing['method'], listing['idempotent'], listing['body_type']))
        self.assertEqual({k: v for k, v in body.items() if k != 'page'}, listing['params'])
        self.assertEqual(0, body['page'])
        self.assertEqual((listing['page_param'], listing['start_page']), ('page', 0))
        self.assertEqual(body['hitsPerPage'], listing['page_size'])
        self.assertEqual(headers['X-Algolia-Application-Id'], site['request_headers']['X-Algolia-Application-Id'])
        from crawler.search_credentials import SEARCH_KEYS
        self.assertEqual({'X-Algolia-API-Key': {'env': SEARCH_KEYS[SITE]}}, site['request_headers_env'])
        self.assertEqual([LIST_HOST], site['request_headers_hosts'])

    def test_plan_holds_no_key_value(self):
        site = self.plan['sites'][SITE]
        self.assertNotIn('X-Algolia-API-Key', site.get('request_headers', {}))
        p = site_plan(self.plan, SITE, registered=[SITE])
        self.assertEqual({'X-Algolia-Application-Id': 'DVUTVWXJFU', 'X-Algolia-API-Key': FAKE_KEY},
                         resolve_headers(p.raw, {KEY_ENV: FAKE_KEY}))
        self.assertEqual([LIST_HOST], header_hosts(p.raw))

    def test_borderline_teaser_takes_longer_detail_like_crawl(self):
        """Teaser of 50..99 chars: crawl() fetches the detail and keeps the longer text."""
        f = copy.deepcopy(self.fixture)
        listing = json.loads(f['listing_body'])
        border = 'Introduction synthétique de longueur moyenne pour le test du seuil.'
        self.assertTrue(50 <= len(border) < 100)
        listing['hits'][0]['rendered_item_listing'] = (
            '<div class="Teaser"><p class="Teaser-intro">%s</p></div>' % border)
        f['listing_body'] = json.dumps(listing, ensure_ascii=False)
        expected, calls = self.native(f)
        self.assertEqual(2, len(expected))
        self.assertEqual(f['long_text'], expected[0]['abstract'])
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(f))
        self.assertEqual(expected, self.raw_papers)

    def test_identity_is_algolia_nid(self):
        expected, _ = self.native()
        self.assertEqual(['900001', '900002'], [p['post_number'] for p in expected])
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in self.pipeline()])


class SyntheticSource:
    """POST-aware transport: robots 404, Algolia JSON POSTs by page, detail GETs."""

    def __init__(self, fixture, listing=None, listing_type='application/json'):
        self.fixture, self.listing, self.listing_type = fixture, listing, listing_type
        self.calls = []

    def request(self, method, url, *, headers=None, data=None):
        self.calls.append((method, url, data, dict(headers or {})))
        if urlsplit(url).path == '/robots.txt':
            return Response(404, {}, b'', url)
        if url in self.fixture['details']:
            return Response(200, {'Content-Type': 'text/html'}, self.fixture['details'][url], url)
        if url == LIST_URL:
            if method != 'POST':
                return Response(405, {'Content-Type': 'application/json'}, '{"message":"Method not allowed"}', url)
            body = json.loads(data.decode() if isinstance(data, bytes) else data)
            if body.get('page', 0) == 0:
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
        with patch.dict(os.environ, {KEY_ENV: FAKE_KEY}):
            headers = resolve_headers(plan)
        with contextlib.redirect_stdout(io.StringIO()):
            result = env.run(plan, None, crawler=crawler, transport=source,
                             extra_headers=headers, header_hosts=header_hosts(plan))
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
        self.assertEqual({'900001', '900002'}, {d['post_number'] for d in docs})
        self.assertEqual(2, len({d['meta_url'] for d in docs}))
        hosts_seen = []
        for method, url, _, headers in source.calls:
            host = urlsplit(url).hostname
            if host not in hosts_seen:
                hosts_seen.append(host)
                self.assertEqual('/robots.txt', urlsplit(url).path)  # robots before anything on a host
            if host == LIST_HOST:
                self.assertEqual(FAKE_KEY, headers.get('X-Algolia-API-Key'))
                self.assertEqual('DVUTVWXJFU', headers.get('X-Algolia-Application-Id'))
            else:
                self.assertNotIn('X-Algolia-API-Key', headers)  # never sent to www.inrae.fr
        posts = [c for c in source.calls if c[1] == LIST_URL]
        self.assertTrue(posts and all(m == 'POST' for m, *_ in posts))

    def test_error_listing_ends_incomplete_as_listing_parse_error(self):
        cases = {'maintenance_page': ('<html><body><p>Site under maintenance</p></body></html>', 'text/html'),
                 'algolia_error_object': (json.dumps({'message': 'Invalid Application-ID or API key',
                                                      'status': 403}), 'application/json')}
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

    def test_missing_key_variable_is_a_plan_error(self):
        from crawler.enumeration.plan import PlanError
        env = {k: v for k, v in os.environ.items() if k != KEY_ENV}
        with self.assertRaises(PlanError):
            resolve_headers(split_plan(SITE), env)


if __name__ == '__main__':
    unittest.main()
