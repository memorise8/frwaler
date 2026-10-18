"""L3b-20261018: enumeration split for defensa-gob-es-gabinete. crawl() reads one press
page per year (current year /gabinete/notasPrensa/, older years /notasPrensa/YYYY/, down to
2007); the plan reads /notasPrensa/{segment}/ for every year as explicit date_range_split
segments (2026-10-18: /notasPrensa/2026/ lists 173 rows, the bare current page only the
latest 40; same absolute detail URLs). ?page=N is ignored, so page 2 repeats page 1."""
import copy
import time
import unittest
from unittest.mock import patch

from tests import collector_split_helpers as helpers
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split, split_fixture, split_plan

SITE = 'defensa-gob-es-gabinete'
YEAR = split_fixture(SITE)['list_year']


def one_year_plan():
    plan = copy.deepcopy(split_plan(SITE))
    plan['segments'] = {'mode': 'explicit', 'items': [{'key': str(YEAR)}]}
    return plan


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def setUp(self):
        super().setUp()
        listing = self.plan['sites'][SITE]['listing']
        listing['url'] = listing['url'].replace('{segment}', str(YEAR))

    def native(self, fixture=None):
        # __init__ (not run here) sets the wall-clock start crawl() reads.
        real_load = helpers.load

        def load_with_start(site):
            crawler, module = real_load(site)
            crawler._crawl_start = time.time()
            return crawler, module
        with patch.object(helpers, 'load', load_with_start):
            return super().native(fixture)

    def test_added_listing_rejects_wrong_shape(self):
        from crawler.enumeration.guard import guarded
        from crawler.enumeration.http import PermanentFailure
        c, _ = load(SITE)
        with self.assertRaises(PermanentFailure) as ctx:
            with guarded():
                c._enumeration_items({'error': 'not a listing'}, self.plan['sites'][SITE]['listing']['url'])
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.http import Response
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?page=1'
        empty = '<html><body><div id="paginacionMINISDEF"><ul class="list-group"></ul></div></body></html>'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, empty, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))

    def test_external_id_is_url_path_like_original(self):
        expected, _ = self.native()
        self.assertEqual(['2026/02/DGC-260211-sintetica-b', '2026/02/DGC-260110-sintetica-a'],
                         [p['external_id'] for p in expected])
        self.assertNotIn('post_number', expected[0])


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def test_error_listing_ends_incomplete_as_listing_parse_error(self):
        result, docs = run_split(SITE, plan=one_year_plan(),
                                 listing='<html><body><p>Site under maintenance</p></body></html>',
                                 listing_type='text/html')
        self.assertEqual('incomplete', result['status'], result)
        self.assertIn('listing_parse_error', result['stop_reason'])
        self.assertEqual([], docs)

    def test_shipped_plan_completes_end_to_end(self):
        from crawler.enumeration.http import Response
        fixture = split_fixture(SITE)
        same = lambda url: Response(200, {'Content-Type': 'text/html'}, fixture['listing_body'], url)
        result, docs = run_split(SITE, plan=one_year_plan(), end=same)
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))
        self.assertEqual(2, len({d['meta_url'] for d in docs}))


if __name__ == '__main__':
    unittest.main()
