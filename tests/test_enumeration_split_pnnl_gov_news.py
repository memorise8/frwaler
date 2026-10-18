"""pnnl-gov-news split: GET /news?news[0]=type:23&page=N (same news_listing view as crawl's
views/ajax POST), detail parsed by crawl's _parse_detail. Memory only."""
import ast
import contextlib
import copy
import hashlib
import io
import json
import unittest
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from crawler.enumeration.urls import build_url
from tests.collector_split_helpers import ROOT, SplitChecks, SplitRunChecks, load

SITE = 'pnnl-gov-news'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, module = load(self.SITE)
        captured, calls, pages = [], [], []

        def post_list(page=0, **kwargs):
            pages.append(page)
            data = fixture['view_html'] if page == 0 else ''
            return json.dumps([{'command': 'settings'}, {'command': 'insert', 'data': data}])

        def get(url, **kwargs):
            calls.append(url)
            self.assertIn(url, fixture['details'])
            return fixture['details'][url]
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(module, '_curl_post_list', post_list))
            stack.enter_context(patch.object(module, '_curl_get', get))
            stack.enter_context(patch('time.sleep', lambda *a: None))
            with contextlib.redirect_stdout(io.StringIO()):
                result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        return captured, calls

    def test_short_abstract_skip_is_preserved(self):
        # crawl() keeps the subtitle in the abstract; remove body, deck and teaser to reach the skip.
        f = copy.deepcopy(self.fixture)
        for url, body in f['details'].items():
            f['details'][url] = body.replace(self.fixture['long_text'], '').replace('Deck of', '')
        for key in ('listing_body', 'view_html'):
            f[key] = f[key].replace('Summary of item-1', '').replace('Summary of item-2', '')
        expected, _ = self.native(f)
        self.assertEqual([], expected)
        self.assertEqual([], self.pipeline(f))

    def test_all_original_methods_are_unchanged(self):
        code = (ROOT / 'crawler/sites/custom' / (self.SITE + '.py')).read_text()
        tree = ast.parse(code)
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
        nodes += [n for c in tree.body if isinstance(c, ast.ClassDef) for n in c.body if isinstance(n, ast.FunctionDef)]
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest() for n in nodes}
        for name, digest in self.fixture['original_function_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        url = build_url(p.listing['url'], p.listing.get('params'))
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['empty_body'], url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        with self.assertRaises(PermanentFailure) as ctx:
            parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html><main>maintenance</main></html>', url))
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_rendered_cards_that_do_not_parse_are_not_the_end(self):
        crawler, _ = load(self.SITE)
        changed = self.fixture['listing_body'].replace('teaser__title', 'card__title')
        with self.assertRaises(PermanentFailure):
            crawler._enumeration_items(changed)


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def test_shipped_plan_completes_end_to_end(self):
        from tests.collector_split_helpers import run_split
        empty = self.fixture['empty_body']
        result, docs = run_split(SITE, end=lambda url: Response(200, {'Content-Type': 'text/html'}, empty, url))
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))
