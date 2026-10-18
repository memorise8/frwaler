"""L3b-20261018: enumeration split for fair-healthdata-be-search (DKAN GET /api/1/search).
The live API ignores crawl()'s start/rows parameters (every offset returns the same
first 10 rows); the plan pages with the API's own page/page-size parameters instead.
crawl() and the identity rule (post_number = dataset identifier) are unchanged.
Past the last page the API answers 200 with an empty result set."""
import copy
import json
import unittest

from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, split_fixture

SITE = 'fair-healthdata-be-search'
EMPTY = split_fixture(SITE)['empty_body']


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_short_abstract_skip_is_preserved(self):
        # The abstract always carries a title preamble and catalogue footer; only a
        # bare record (short title, short id, nothing else) falls below 50 chars.
        body = {'total': '2', 'results': {'a': {'identifier': 'a1', 'title': 'T'},
                                          'b': {'identifier': 'b2', 'title': 'U'}}}
        f = copy.deepcopy(self.fixture)
        f['listing_body'] = json.dumps(body)
        expected, _ = self.native(f)
        self.assertEqual([], expected)
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.http import Response
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        from types import SimpleNamespace
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        adapter = Adapter(self.SITE, p.section('adapter'), None, crawler=crawler)
        items = ListingParser(p.listing['parser'], adapter=adapter).parse(
            Response(200, {'Content-Type': 'application/json'}, f['listing_body'], p.listing['url'])).items
        self.assertEqual(2, len(items))
        self.assertEqual([None, None], [adapter.materialize(i, SimpleNamespace(get=None)) for i in items])

    def test_valid_empty_page_and_error_page_are_distinct(self):
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.http import PermanentFailure, Response
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?fulltext=&page-size=20&page=16'
        page = parser.parse(Response(200, {'Content-Type': 'application/json'}, EMPTY, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))
        with self.assertRaises(PermanentFailure):
            parser.parse(Response(200, {'Content-Type': 'application/json'}, '{"message": "error"}', url))

    def test_post_number_is_identifier_like_original(self):
        expected, _ = self.native()
        self.assertEqual(['00000000-0000-4000-8000-000000009001', '00000000-0000-4000-8000-000000009000'],
                         [p['post_number'] for p in expected])

    def test_plan_pages_with_page_and_page_size(self):
        listing = self.plan['sites'][SITE]['listing']
        self.assertEqual(('page', 1, 20), (listing['page_param'], listing['start_page'], listing['params']['page-size']))
        self.assertNotIn('page_mode', listing)


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{SITE: (200, 'application/json', EMPTY)})


if __name__ == '__main__':
    unittest.main()
