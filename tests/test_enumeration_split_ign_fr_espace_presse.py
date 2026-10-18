"""ign-fr-espace-presse: sitemap plan + pure detail conversion (memory only).

The site has no listing pagination (two static listing pages); the shipped plan reads
the Drupal sitemap with an include filter and converts each detail page with
``_enumeration_paper(html, url)``, which must equal what the unchanged crawl() saves.
"""
import ast
import copy
import hashlib
import json
import unittest
from types import SimpleNamespace

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from crawler.enumeration.strategies import Sitemap
from crawler.enumeration.urls import normalize_key_url
from tests.collector_split_helpers import ROOT, SplitChecks, SplitRunChecks, load, split_fixture, split_plan

SITE = 'ign-fr-espace-presse'


def sitemap_items(plan_site, body):
    """Items exactly as the Sitemap strategy builds them (include/exclude applied)."""
    import xml.etree.ElementTree as ET
    strat = Sitemap.__new__(Sitemap)
    strat.cfg = plan_site['sitemap']
    import re
    strat.include = [re.compile(p) for p in strat.cfg.get('include', [])]
    strat.exclude = [re.compile(p) for p in strat.cfg.get('exclude', [])]
    from crawler.enumeration.parsing import Item
    out = []
    for node in ET.fromstring(body):
        loc = next(((c.text or '').strip() for c in node if c.tag.rsplit('}', 1)[-1] == 'loc'), '')
        if loc and strat.wanted(loc):
            out.append(Item(key=normalize_key_url(loc), url=loc, date=None, payload={'detail_url': loc}))
    return out


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

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
        # The sitemap selection equals the URLs the registered listing parser yields.
        listed = ListingParser({'type': 'adapter', 'url_field': 'url'}, adapter=adapter).parse(
            Response(200, {'Content-Type': 'text/html'}, fixture['listing_body'], 'https://www.ign.fr/espace-presse')).items
        self.assertEqual([i.url for i in listed], [i.url for i in items])
        papers, calls = [], []
        def fetch(url):
            self.assertIn(url, fixture['details'], 'undeclared detail request')
            calls.append(url)
            return Response(200, {'Content-Type': 'text/html'}, fixture['details'][url],
                            url + '?moved=1' if redirect else url)
        for item in items:
            paper = adapter.materialize(item, SimpleNamespace(get=fetch))
            if paper is not None:
                papers.append(paper)
        self.assertEqual(len(fixture['details']), len(calls))
        return papers

    def test_module_helpers_are_unchanged(self):
        code = (ROOT/'crawler/sites/custom'/(self.SITE+'.py')).read_text()
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in ast.parse(code).body if isinstance(n, ast.FunctionDef)}
        self.assertEqual(self.fixture['module_function_source_sha256'], actual)

    def test_identity_is_drupal_node_id(self):
        papers = self.pipeline()
        self.assertEqual(['901', '902'], [paper_identity(p)[0] for p in papers])
        self.assertEqual(list(self.fixture['details']), [paper_identity(p)[1] for p in papers])

    def test_listing_without_press_links_is_an_error(self):
        crawler, _ = load(self.SITE)
        for body in ('<html><main></main></html>', '<html><body>maintenance</body></html>'):
            with self.assertRaises(PermanentFailure) as ctx:
                with guarded():
                    crawler._enumeration_items(body, 'https://www.ign.fr/espace-presse')
            self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        """Sitemap plan: no listing pages. The distinction lives in the sitemap strategy:
        a urlset without wanted URLs is a filtering result, a non-XML page fails the run
        (both are covered end-to-end in RegisteredSplitRun)."""
        self.assertEqual('sitemap', self.plan['sites'][self.SITE]['strategy'])
        self.assertNotIn('listing', self.plan['sites'][self.SITE])


def run_sitemap(fixture, *, sitemap_body=None, sitemap_type='application/xml'):
    import contextlib, io
    from tests.enumeration_fakes import Env
    from urllib.parse import urlsplit
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
        self.assertEqual(['901', '902'], [d['post_number'] for d in docs])
        self.assertEqual(list(self.fixture['details']), [d['meta_url'] for d in docs])
        # Hub pages and non-press pages were never requested.
        urls = result['transport'].urls
        self.assertFalse([u for u in urls if u.endswith(('/espace-presse', 'archives', 'notre-histoire'))], urls)

    def test_error_listing_ends_incomplete_as_listing_parse_error(self):
        """Sitemap counterpart: a maintenance page instead of the sitemap is a failed
        sitemap (incomplete), never a completed empty source."""
        result, docs = run_sitemap(self.fixture, sitemap_body='<html><body>maintenance</body></html>',
                                   sitemap_type='text/html')
        self.assertEqual('incomplete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('sitemaps_failed:'), result)
        self.assertEqual([], docs)

    def test_sitemap_without_press_urls_is_not_complete_with_items(self):
        body = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                '<url><loc>https://www.ign.fr/espace-presse</loc></url></urlset>')
        result, docs = run_sitemap(self.fixture, sitemap_body=body)
        self.assertEqual('complete_unverified', result['status'], result)
        self.assertEqual([], docs)

    def test_short_paper_does_not_silently_become_complete(self):
        fixture = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        result, docs = run_sitemap(fixture)
        self.assertEqual('complete_with_review', result['status'], result)
        self.assertEqual([], docs)
