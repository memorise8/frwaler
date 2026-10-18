"""icn-artem-com-en: sitemap plan + pure detail conversion (memory only).

crawl() discovers URLs from the /en/news/ page plus post-sitemap.xml (English
posts minus hub prefixes) and converts each detail page. The shipped plan reads the
same sitemap with an equivalent include/exclude filter and converts each detail page
with ``_enumeration_paper(html, request_url)``, which must equal crawl()'s paper.
"""
import contextlib
import copy
import io
import json
import re
import unittest
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import Item
from crawler.enumeration.plan import site_plan
from crawler.enumeration.urls import normalize_key_url
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, split_plan

SITE = 'icn-artem-com-en'


def sitemap_items(plan_site, body):
    """Items as the Sitemap strategy builds them (Sitemap.wanted semantics)."""
    cfg = plan_site['sitemap']
    include = [re.compile(p) for p in cfg.get('include', [])]
    exclude = [re.compile(p) for p in cfg.get('exclude', [])]
    out = []
    for node in ET.fromstring(body):
        loc = next(((c.text or '').strip() for c in node if c.tag.rsplit('}', 1)[-1] == 'loc'), '')
        if loc and (not include or any(p.search(loc) for p in include)) and not any(p.search(loc) for p in exclude):
            out.append(Item(key=normalize_key_url(loc), url=loc, date=None, payload={'detail_url': loc}))
    return out


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        captured = []; calls = []

        def get(url, *args, **kwargs):
            calls.append(url)
            if url == fixture['news_url']:
                return fixture['listing_body']
            if url == fixture['sitemap_url']:
                return fixture['sitemap_body']
            return fixture['details'][url]

        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_get = get
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        return captured, calls

    def pipeline(self, fixture=None, redirect=False):
        fixture = fixture or self.fixture
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        adapter = Adapter(self.SITE, p.section('adapter'), None, crawler=crawler)
        self.raw_papers = []
        original = crawler._enumeration_paper
        def capture(*args, **kwargs):
            result = original(*args, **kwargs)
            if result is not None:
                self.raw_papers.append(copy.deepcopy(result))
            return result
        crawler._enumeration_paper = capture
        items = sitemap_items(self.plan['sites'][self.SITE], fixture['sitemap_body'])
        self.assertEqual(2, len(items))
        papers, calls = [], []
        def fetch(url):
            self.assertIn(url, fixture['details'], 'undeclared detail request')
            calls.append(url)
            resp = Response(200, {'Content-Type': 'text/html'}, fixture['details'][url],
                            url + '?moved=1' if redirect else url)
            return resp
        for item in items:
            paper = adapter.materialize(item, SimpleNamespace(get=fetch))
            if paper is not None:
                papers.append(paper)
        self.assertEqual(len(fixture['details']), len(calls))
        return papers

    def test_sitemap_selection_equals_crawl_discovery(self):
        _, calls = self.native()
        detail_calls = [u for u in calls if u in self.fixture['details']]
        items = sitemap_items(self.plan['sites'][self.SITE], self.fixture['sitemap_body'])
        self.assertEqual(detail_calls, [i.url for i in items])

    def test_identity_is_wordpress_post_id(self):
        papers = self.pipeline()
        self.assertEqual(['771', '772'], [paper_identity(p)[0] for p in papers])
        self.assertEqual(list(self.fixture['details']), [paper_identity(p)[1] for p in papers])

    def test_added_listing_rejects_wrong_shape(self):
        crawler, _ = load(self.SITE)
        for body in ({'error': 'not a listing'}, '<html><body>maintenance</body></html>'):
            with self.assertRaises(PermanentFailure) as ctx:
                with guarded():
                    crawler._enumeration_items(body, self.fixture['news_url'])
            self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        """Sitemap plan: no listing pages; a urlset without wanted URLs versus a non-XML
        page is covered end-to-end in RegisteredSplitRun."""
        self.assertEqual('sitemap', self.plan['sites'][self.SITE]['strategy'])
        self.assertNotIn('listing', self.plan['sites'][self.SITE])


def run_sitemap(fixture, *, sitemap_body=None, sitemap_type='application/xml'):
    from tests.enumeration_fakes import Env
    plan = copy.deepcopy(split_plan(SITE))
    plan['request_interval_s'] = 0
    def handler(url):
        if urlsplit(url).path == '/robots.txt':
            return Response(404, {}, b'')
        if url == fixture['sitemap_url']:
            return Response(200, {'Content-Type': sitemap_type},
                            fixture['sitemap_body'] if sitemap_body is None else sitemap_body, url)
        if url in fixture['details']:
            return Response(200, {'Content-Type': 'text/html'}, fixture['details'][url], url)
        return Response(404, {'Content-Type': 'text/html'}, '<html>not found</html>', url)
    env = Env(SITE)
    try:
        _, module = load(SITE)
        cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
        crawler = cls(env.conn)
        with contextlib.redirect_stdout(io.StringIO()):
            result = env.run(plan, handler, crawler=crawler)
        return result, env.docs()
    finally:
        env.close()


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def test_shipped_plan_completes_end_to_end(self):
        result, docs = run_sitemap(self.fixture)
        self.assertEqual('complete', result['status'], result)
        self.assertEqual('all_sitemaps_listed', result['stop_reason'], result)
        self.assertEqual(['771', '772'], [d['post_number'] for d in docs])
        self.assertEqual(list(self.fixture['details']), [d['meta_url'] for d in docs])
        urls = result['transport'].urls
        self.assertFalse([u for u in urls if '/students/' in u or u.endswith('/en/news/')], urls)

    def test_error_listing_ends_incomplete_as_listing_parse_error(self):
        result, docs = run_sitemap(self.fixture, sitemap_body='<html><body>maintenance</body></html>',
                                   sitemap_type='text/html')
        self.assertEqual('incomplete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('sitemaps_failed:'), result)
        self.assertEqual([], docs)

    def test_sitemap_without_english_posts_is_not_complete_with_items(self):
        body = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                '<url><loc>https://www.icn-artem.com/candidats/actualites/</loc></url></urlset>')
        result, docs = run_sitemap(self.fixture, sitemap_body=body)
        self.assertEqual('complete_unverified', result['status'], result)
        self.assertEqual([], docs)

    def test_short_paper_does_not_silently_become_complete(self):
        fixture = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        result, docs = run_sitemap(fixture)
        self.assertEqual('complete_with_review', result['status'], result)
        self.assertEqual([], docs)
