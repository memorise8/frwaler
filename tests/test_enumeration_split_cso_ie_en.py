"""L3b-20261018: enumeration split for cso-ie-en. crawl() walks one press-release page per
year (/en/csolatestnews/pressreleases/{year}pressreleases/, current year down to 2010); the
plan reads the same pages as explicit date_range_split segments ({segment} = year). The page
ignores ?page=N, so page 2 repeats page 1 and the forward pager ends the segment there.
external_id/post_number stay the list anchor node id (None for the current-year monthly
module links, exactly as crawl())."""
import copy
import json
import unittest
from datetime import datetime
from unittest.mock import patch

from tests import collector_split_helpers as helpers
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split, split_fixture, split_plan

SITE = 'cso-ie-en'
YEAR = split_fixture(SITE)['list_year']


class _FixedNow(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(YEAR, 6, 1)


def one_year_plan():
    plan = copy.deepcopy(split_plan(SITE))
    plan['segments'] = {'mode': 'explicit', 'items': [{'key': str(YEAR)}]}
    return plan


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def setUp(self):
        super().setUp()
        # One concrete year page (the plan's {segment} placeholder filled in).
        listing = self.plan['sites'][SITE]['listing']
        listing['url'] = listing['url'].replace('{segment}', str(YEAR))

    def native(self, fixture=None):
        # crawl() over a single year: current year (fixed) == earliest year.
        real_load = helpers.load

        def load_one_year(site):
            crawler, module = real_load(site)
            module.datetime = _FixedNow
            module._EARLIEST_YEAR = YEAR
            return crawler, module
        with patch.object(helpers, 'load', load_one_year):
            return super().native(fixture)

    def test_added_listing_rejects_wrong_shape(self):
        from crawler.enumeration.guard import guarded
        from crawler.enumeration.http import PermanentFailure
        c, _ = load(SITE)
        url = self.plan['sites'][SITE]['listing']['url']
        for args in ([{'error': 'not a listing'}, url], ['<html></html>', 'https://www.cso.ie/en/other/']):
            with self.assertRaises(PermanentFailure) as ctx:
                with guarded():
                    c._enumeration_items(*args)
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
        empty = '<html><body><div class="row mainContent"><div class="copy"></div></div></body></html>'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, empty, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))

    def test_post_number_is_node_id_like_original(self):
        expected, _ = self.native()
        self.assertEqual(['990002', '990001'], [p['external_id'] for p in expected])
        self.assertEqual([YEAR, YEAR], [json.loads(p['metadata'])['year'] for p in expected])

    def test_monthly_module_link_keeps_none_identity(self):
        crawler, _ = load(SITE)
        body = ('<html><body><div class="mainContent conferencePapers"><div class="moduleBody">'
                f'<a id="3_link_1" href="/en/csolatestnews/pressreleases/{YEAR}pressreleases/pressstatement-syn/">'
                f'Synthetic Release - 11 February {YEAR}</a></div></div></body></html>')
        rows = crawler._enumeration_items(body, f'https://www.cso.ie/en/csolatestnews/pressreleases/{YEAR}pressreleases/')
        self.assertEqual(1, len(rows))
        for row in rows:
            self.assertIsNone(row['node_id'])
            self.assertEqual(YEAR, row['year'])


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
        # ?page=2 serves the same year page again (the site ignores the parameter).
        fixture = split_fixture(SITE)
        from crawler.enumeration.http import Response
        same = lambda url: Response(200, {'Content-Type': 'text/html'}, fixture['listing_body'], url)
        result, docs = run_split(SITE, plan=one_year_plan(), end=same)
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(2, len(docs))
        self.assertEqual({'990002', '990001'}, {d['post_number'] for d in docs})
        self.assertEqual(2, len({d['meta_url'] for d in docs}))


if __name__ == '__main__':
    unittest.main()
