"""publicservice-govt-nz-research-and-data split: listing rows (topic 193 Research datasheets,
start offset 25) + crawl's record parsers; PDF rows use the response headers instead of a HEAD."""
import unittest

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from crawler.enumeration.urls import build_url
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split

SITE = 'publicservice-govt-nz-research-and-data'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        url = build_url(p.listing['url'], p.listing.get('params'))
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['empty_body'], url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html><main>maintenance</main></html>', url))
        self.assertTrue(page.extra.get('parse_error'))
        self.assertFalse(page.extra.get('explicit_empty', False))

    def test_rendered_results_that_do_not_parse_are_not_the_end(self):
        crawler, _ = load(self.SITE)
        changed = self.fixture['listing_body'].replace('result__title-link', 'card-link')
        with self.assertRaises(PermanentFailure):
            crawler._enumeration_items(changed)


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def test_shipped_plan_completes_end_to_end(self):
        empty = self.fixture['empty_body']
        result, docs = run_split(SITE, end=lambda url: Response(200, {'Content-Type': 'text/html'}, empty, url))
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(2, len(docs))
        self.assertEqual({'publicservice-2933', 'publicservice-1194'}, {d['post_number'] for d in docs})

    def test_short_paper_does_not_silently_become_complete(self):
        import copy
        fixture = copy.deepcopy(self.fixture)
        for key in ('listing_body',):
            fixture[key] = fixture[key].replace(self.fixture['long_text'], 'Short.')
        for url in fixture['details']:
            fixture['details'][url] = fixture['details'][url].replace(self.fixture['long_text'], 'Short.')
        empty = self.fixture['empty_body']
        result, docs = run_split(SITE, fixture=fixture, end=lambda url: Response(200, {'Content-Type': 'text/html'}, empty, url))
        self.assertEqual('complete_with_review', result['status'], result)
        self.assertEqual([], docs)
