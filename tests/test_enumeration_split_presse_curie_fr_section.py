"""presse-curie-fr-section: WordPress admin-ajax list fragments read by POST (ext:post_listing)
with the nonce read once per run from the public list page (ext:dynamic_token), offset paging,
a blank 200 body past the end (ext:blank_body_end, X3) and crawl()'s short-abstract skip as a
rule skip (ext:none_is_rule_skip, X3). crawl() (unchanged) and the shipped plan are compared
on synthetic data through the real SiteRunner; no network, process or customer database."""
import ast
import contextlib
import copy
import hashlib
import io
import json
import re
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from crawler.enumeration.adapters import clean_paper
from crawler.enumeration.http import PermanentFailure, Response
from tests.collector_split_helpers import ROOT, load, split_fixture, split_plan
from tests.enumeration_fakes import Env
from tests.test_enumeration_extensions import RecordingTransport

SITE = 'presse-curie-fr-section'
AJAX = 'https://presse.curie.fr/wp-admin/admin-ajax.php'
LIST_PAGE = 'https://presse.curie.fr/section/communiques/?lang=en'


def short(fixture):
    """Article bodies (and the og:description fallback) under crawl()'s 50-character rule."""
    f = copy.deepcopy(fixture)
    f['details'] = {u: re.sub(r'<div class="templateCp-content">.*?</div>', '<div class="templateCp-content"><p>Short.</p></div>', h)
                    for u, h in f['details'].items()}
    return f


def by_post(rows):
    return sorted(rows, key=lambda r: r['post_number'])


class CurieSplit(unittest.TestCase):
    def setUp(self):
        self.fixture = split_fixture(SITE)
        self.plan = split_plan(SITE)
        denials = []

        def denied(*args, **kwargs):
            denials.append('attempted I/O')
            raise AssertionError('split test attempted network/process I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'socket.socket.connect_ex',
                     'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)
        self.addCleanup(lambda: self.assertEqual([], denials))

    # -- the registered crawl() with memory curl helpers -------------------------------
    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, _ = load(SITE)
        captured, posts = [], []

        def get(url, context='GET'):
            if url == LIST_PAGE:
                return fixture['nonce_page']
            return fixture['details'][url]

        def post(url, data, context='POST'):
            self.assertEqual(AJAX, url)
            form = parse_qs(data)
            posts.append(form)
            self.assertEqual([fixture['nonce']], form['security'])
            return fixture['listing_body'] if form['offset'] == ['0'] else fixture['empty_body']
        crawler.detail_delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_get = get
        crawler._curl_post = post
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.posts = posts
        return captured

    # -- the shipped plan through the real runner -------------------------------------
    def handler(self, fixture, end_body=None, seen=None):
        def handle(method, url, data):
            parts = urlsplit(url)
            if parts.path == '/robots.txt':
                return Response(404, {}, b'')
            if method == 'GET' and url == LIST_PAGE:
                return Response(200, {'Content-Type': 'text/html'}, fixture['nonce_page'], url)
            if method == 'GET' and url in fixture['details']:
                return Response(200, {'Content-Type': 'text/html'}, fixture['details'][url], url)
            self.assertEqual(('POST', AJAX), (method, url))
            form = parse_qs(data.decode() if isinstance(data, bytes) else data)
            if seen is not None:
                seen.append(form)
            self.assertEqual([fixture['nonce']], form['security'])
            body = fixture['listing_body'] if form['offset'] == ['0'] else (
                fixture['empty_body'] if end_body is None else end_body)
            return Response(200, {'Content-Type': fixture['content_type']}, body, url)
        return handle

    def run_plan(self, fixture=None, plan=None, end_body=None, seen=None):
        fixture = fixture or self.fixture
        plan = copy.deepcopy(plan or self.plan)
        plan['request_interval_s'] = 0
        env = Env(SITE)
        self.addCleanup(env.close)
        _, module = load(SITE)
        cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
        crawler = cls(env.conn, 0)
        raw = []
        original = crawler._enumeration_paper

        def capture(*args, **kwargs):
            paper = original(*args, **kwargs)
            if paper is not None:
                raw.append(copy.deepcopy(paper))
            return paper
        crawler._enumeration_paper = capture
        transport = RecordingTransport(self.handler(fixture, end_body, seen))
        with contextlib.redirect_stdout(io.StringIO()):
            result = env.run(plan, None, crawler=crawler, transport=transport)
        return result, env.docs(), raw, transport

    def test_plan_posts_the_same_query_as_crawl(self):
        self.native()
        seen = []
        self.run_plan(seen=seen)
        crawl_first = {k: v for k, v in self.posts[0].items() if k not in ('action',)}
        plan_first = {k: v for k, v in seen[0].items() if k not in ('action',)}
        self.assertEqual(crawl_first, plan_first)
        # crawl() asks offset 0 with mon_action_Post and later offsets with load_more_post; the
        # site answers load_more_post at offset 0 with the same fragment (live check 2026-10-18).
        self.assertEqual(['mon_action_Post'], self.posts[0]['action'])
        self.assertTrue(all(f['action'] == ['load_more_post'] for f in seen))
        self.assertIn(['10'], [f['offset'] for f in seen])  # page 2 = offset 10, as crawl() pages

    def test_full_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(2, len(expected))
        result, docs, raw, _ = self.run_plan()
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        # last_page_backward reads the list from its end, so compare by post number.
        self.assertEqual(by_post(expected), by_post(raw), 'raw paper must equal what crawl() saves')
        docs = by_post(docs)
        self.assertEqual([p['post_number'] for p in by_post(expected)], [d['post_number'] for d in docs])
        self.assertEqual([clean_paper(p)['title'] for p in by_post(expected)], [d['title'] for d in docs])
        self.assertEqual([p['pdf_url'] for p in by_post(expected)], [d['pdf_url'] for d in docs])
        self.assertEqual(2, len({d['meta_url'] for d in docs}))

    def test_short_abstract_is_a_rule_skip(self):
        f = short(self.fixture)
        self.assertEqual([], self.native(f))
        result, docs, raw, _ = self.run_plan(f)
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(2, result['stats']['skipped_by_rule'])
        self.assertEqual([], docs)

    def test_blank_end_needs_the_declaration(self):
        plan = copy.deepcopy(self.plan)
        del plan['listing']['end_on_blank_body']
        result, _, _, _ = self.run_plan(plan=plan)
        self.assertNotEqual('complete', result['status'], result)

    def test_non_blank_foreign_body_is_not_an_end(self):
        result, _, _, _ = self.run_plan(end_body='<html><body>maintenance</body></html>')
        self.assertNotEqual('complete', result['status'], result)

    def test_listing_rejects_wrong_shape(self):
        crawler, _ = load(SITE)
        for body in ('<html><body>error</body></html>', '0', None):
            with self.assertRaises(PermanentFailure) as ctx:
                crawler._enumeration_items(body)
            self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_nonce_is_not_recorded_in_requests_urls(self):
        _, _, _, transport = self.run_plan()
        self.assertFalse(any(self.fixture['nonce'] in url for url in transport.urls))

    def test_all_original_methods_are_unchanged(self):
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fixture['original_method_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)


if __name__ == '__main__':
    unittest.main()
