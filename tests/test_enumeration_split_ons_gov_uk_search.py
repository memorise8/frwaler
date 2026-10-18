"""L3b-20261018: enumeration split for ons-gov-uk-search.
The site's own search backend api.beta.ons.gov.uk/v1/search (q=uk wide,
content_type=dataset, the crawler's scope) returns the whole result set (count 665)
with limit=1000; relevance order is not stable between calls, so one offset page of
1000 is read and offset 1000 is the empty end page. Detail = {uri}/data JSON on
www.ons.gov.uk, conversion = _process_item() copied verbatim.
Memory-only: no network or subprocess (the end-to-end run uses a temporary SQLite)."""
import json
import unittest

from crawler.enumeration.http import Response
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load

SITE = 'ons-gov-uk-search'
EMPTY = json.dumps({'count': 2, 'took': 1, 'items': []})


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_valid_empty_page_and_error_page_are_distinct(self):
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.http import PermanentFailure
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], total_cfg=p.listing.get('total'),
                               adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?q=uk+wide&limit=1000&content_type=dataset&offset=1000'
        page = parser.parse(Response(200, {'Content-Type': 'application/json'}, EMPTY, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        self.assertEqual(2, page.total)
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))
        with self.assertRaises(PermanentFailure):
            parser.parse(Response(200, {'Content-Type': 'application/json'}, '[]', url))
        with self.assertRaises(PermanentFailure):
            parser.parse(Response(200, {'Content-Type': 'application/json'},
                                  json.dumps({'count': 1, 'items': [{'title': 'no uri'}]}), url))

    def test_identity_like_original(self):
        expected, _ = self.native()
        self.assertEqual(['syntheticone/current', 'synthetictwo/2015'], [p['external_id'] for p in expected])
        self.assertEqual(['SYN001', 'synthetictwo/2015'], [p['post_number'] for p in expected])

    def test_plan_scope_is_the_crawler_scope(self):
        _, module = load(SITE)
        params = self.plan['sites'][SITE]['listing']['params']
        self.assertEqual(('dataset',), module._CONTENT_TYPES)
        self.assertEqual({'q': 'uk wide', 'limit': 1000, 'content_type': 'dataset'}, params)


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{SITE: (200, 'application/json', EMPTY)})


if __name__ == '__main__':
    unittest.main()
