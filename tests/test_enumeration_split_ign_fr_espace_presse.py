"""ign-fr-espace-presse: registered scope as explicit segments + pure conversion (memory only).

The registered crawler reads exactly two listing pages (``/espace-presse`` and
``/espace-presse/communiques-de-presse-archives``; no pagination, ``?page`` is ignored)
and fetches every linked article. The shipped plan expresses that scope as a
``date_range_split`` with two explicit segments substituted into ``listing.url``
(``{segment}``). Inside a segment ``?page=1`` repeats ``?page=0`` exactly, which the
forward pager ends as ``repeated_page`` after re-reading the repeat and checking the
next page brings nothing new. The two pages are a union, not a partition, so the plan
sets ``allow_overlap`` (crawl() de-duplicates across the two pages the same way).
``_enumeration_paper(html, url)`` must equal what the unchanged crawl() saves.
"""
import ast
import contextlib
import copy
import hashlib
import io
import json
import unittest
from urllib.parse import urlsplit

from crawler.enumeration.adapters import Adapter, paper_identity
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from crawler.enumeration.strategies import DateRangeSplit, generate_segments
from tests.collector_split_helpers import ROOT, SplitChecks, SplitRunChecks, load, split_plan

SITE = 'ign-fr-espace-presse'
MAIN = 'espace-presse'
ARCHIVES = 'espace-presse/communiques-de-presse-archives'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_module_helpers_are_unchanged(self):
        code = (ROOT/'crawler/sites/custom'/(self.SITE+'.py')).read_text()
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in ast.parse(code).body if isinstance(n, ast.FunctionDef)}
        self.assertEqual(self.fixture['module_function_source_sha256'], actual)

    def test_identity_is_drupal_node_id(self):
        papers = self.pipeline()
        self.assertEqual(['901', '902'], [paper_identity(p)[0] for p in papers])
        self.assertEqual(list(self.fixture['details']), [paper_identity(p)[1] for p in papers])

    def test_segments_are_the_registered_listing_pages(self):
        """The explicit segments resolve to exactly the crawler's _LISTING_URLS, in order."""
        _, module = load(self.SITE)
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        strat = DateRangeSplit.__new__(DateRangeSplit)
        strat.listing, strat.cfg = p.listing, p.section('segments')
        keys = generate_segments(strat.cfg, None)
        self.assertEqual([MAIN, ARCHIVES], keys)
        self.assertEqual(module._LISTING_URLS, [strat.segment_url(k) for k in keys])
        self.assertEqual([{}, {}], [strat.filter_params(k) for k in keys])
        self.assertTrue(strat.cfg.get('allow_overlap'))

    def test_listing_without_press_links_is_an_error(self):
        crawler, _ = load(self.SITE)
        for body in ('<html><main></main></html>', '<html><body>maintenance</body></html>'):
            with self.assertRaises(PermanentFailure) as ctx:
                with guarded():
                    crawler._enumeration_items(body, 'https://www.ign.fr/espace-presse')
            self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        """Both registered pages always carry press links and ignore ?page, so an empty
        page never ends a segment (the end is the exact repeat of ?page=0): an empty <main>
        and a foreign page are both listing errors, never an explicit empty page."""
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None,
                                                                     crawler=crawler))
        for body in (self.fixture['empty_body'], '<html>maintenance</html>'):
            with self.assertRaises(PermanentFailure) as ctx:
                parser.parse(Response(200, {'Content-Type': 'text/html'}, body, 'https://www.ign.fr/espace-presse?page=0'))
            self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'), ctx.exception.reason)


def run_segments(fixture, *, bodies=None, plan=None):
    """Real SiteRunner.run over the shipped plan. Each segment URL answers its own body for
    any ?page (as the live site does); details from the fixture; anything else 404."""
    from tests.enumeration_fakes import Env
    plan = copy.deepcopy(plan or split_plan(SITE))
    plan['request_interval_s'] = 0
    bodies = bodies or fixture['segment_bodies']
    def handler(url):
        parts = urlsplit(url)
        if parts.path == '/robots.txt':
            return Response(404, {}, b'')
        if url in fixture['details']:
            return Response(200, {'Content-Type': 'text/html'}, fixture['details'][url], url)
        key = parts.path.lstrip('/')
        if key in bodies:
            return Response(200, {'Content-Type': 'text/html'}, bodies[key], url)
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

    def listing_urls(self, result):
        return [u for u in result['transport'].urls if '?page=' in u]

    def test_shipped_plan_completes_end_to_end(self):
        result, docs = run_segments(self.fixture)
        self.assertEqual('complete', result['status'], result)
        self.assertEqual('all_segments_reconciled', result['stop_reason'], result)
        self.assertEqual({'901', '902'}, {d['post_number'] for d in docs})
        self.assertEqual(set(self.fixture['details']), {d['meta_url'] for d in docs})
        # Per segment: page 0, page 1 (repeat), page 1 re-read, page 2 (nothing new) -> end.
        base = 'https://www.ign.fr/'
        expected = [f'{base}{k}?page={n}' for k in (MAIN, ARCHIVES) for n in (0, 1, 1, 2)]
        self.assertEqual(expected, self.listing_urls(result))

    def test_item_on_both_pages_is_saved_once(self):
        """An article linked from both registered pages: crawl() keeps one; so does the plan
        (allow_overlap: the union is the scope, not a partition)."""
        bodies = {MAIN: self.fixture['listing_body'], ARCHIVES: self.fixture['segment_bodies'][ARCHIVES]}
        result, docs = run_segments(self.fixture, bodies=bodies)
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(2, len(docs))
        details = [u for u in result['transport'].urls if u in self.fixture['details']]
        self.assertEqual(sorted(self.fixture['details']), sorted(details))

    def test_overlap_without_allow_overlap_is_reported(self):
        plan = copy.deepcopy(split_plan(SITE))
        del plan['segments']['allow_overlap']
        bodies = {MAIN: self.fixture['listing_body'], ARCHIVES: self.fixture['segment_bodies'][ARCHIVES]}
        result, _ = run_segments(self.fixture, bodies=bodies, plan=plan)
        self.assertEqual('complete_with_mismatch', result['status'], result)
        self.assertIn('segment_overlap', result['stop_reason'])

    def test_archives_answering_the_main_page_is_not_complete(self):
        """If the archives URL served the main page (redirect, removed page), the identical
        first pages are flagged instead of being counted as a reconciled scope."""
        main = self.fixture['segment_bodies'][MAIN]
        result, _ = run_segments(self.fixture, bodies={MAIN: main, ARCHIVES: main})
        self.assertEqual('complete_unverified', result['status'], result)

    def test_missing_archives_page_is_not_complete(self):
        result, _ = run_segments(self.fixture, bodies={MAIN: self.fixture['segment_bodies'][MAIN]})
        self.assertNotIn(result['status'], ('complete', 'complete_with_review'), result)

    def test_error_listing_ends_incomplete_as_listing_parse_error(self):
        body = '<html><body><p>Site under maintenance</p></body></html>'
        result, docs = run_segments(self.fixture, bodies={MAIN: body, ARCHIVES: body})
        self.assertEqual('incomplete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('listing_failed:listing_parse_error:'), result)
        self.assertEqual([], docs)

    def test_short_paper_does_not_silently_become_complete(self):
        fixture = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        result, docs = run_segments(fixture)
        self.assertEqual('complete_with_review', result['status'], result)
        self.assertEqual([], docs)
