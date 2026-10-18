"""L3b-20261018: enumeration split for auswaertiges-amt-de-de.
GET json-filterlist?offset=N (item offset, 20 rows per page, newest first).
Past the end the source answers 200 with {"items": []} (itemsTotal kept)."""
import json
import unittest

from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load

SITE = 'auswaertiges-amt-de-de'
EMPTY = json.dumps({'itemsTotal': 2, 'sortDirection': 'desc', 'items': [], 'facets': []})


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_valid_empty_page_and_error_page_are_distinct(self):
        # The real end page is a JSON object with an empty items list (not a bare []).
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.http import PermanentFailure, Response
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?offset=720'
        page = parser.parse(Response(200, {'Content-Type': 'text/json;charset=UTF-8'}, EMPTY, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))
        with self.assertRaises(PermanentFailure):
            parser.parse(Response(200, {'Content-Type': 'application/json'}, '[]', url))

    def test_post_number_is_numeric_link_id_like_original(self):
        expected, _ = self.native()
        self.assertEqual(['9000102', '9000101'], [p['external_id'] for p in expected])
        self.assertEqual(['9000102', '9000101'], [p['post_number'] for p in expected])


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{SITE: (200, 'application/json', EMPTY)})


if __name__ == '__main__':
    unittest.main()
