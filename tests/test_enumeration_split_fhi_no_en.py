"""L3b-20261018: enumeration split for fhi-no-en.

List: GET /api/search?type=con-32/cat-750&lang=en&sort=1&pageSize=25&page=N (JSON,
totalMatching). Past the last page the API answers 200 with "results": [].
The crawler fetches through self._curl (subprocess); the native run routes it
to the fixture transport instead."""
import json
import unittest
from unittest.mock import patch

import tests.collector_split_helpers as helpers
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load

SITE = 'fhi-no-en'
EMPTY = json.dumps({'totalMatching': 2, 'pageSize': 25, 'results': []})


def _load_with_curl_routed(site):
    # load() executes the module afresh, so only this run's class is changed.
    crawler, module = load(site)
    type(crawler)._curl = lambda self, url, **kw: self._curl_get(url)
    return crawler, module


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        with patch.object(helpers, 'load', _load_with_curl_routed):
            return super().native(fixture)

    def test_valid_empty_page_and_error_page_are_distinct(self):
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.http import PermanentFailure, Response
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], total_cfg=p.listing.get('total'),
                               adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?type=con-32%2Fcat-750&lang=en&sort=1&pageSize=25&page=12'
        page = parser.parse(Response(200, {'Content-Type': 'application/json'}, EMPTY, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))
        with self.assertRaises(PermanentFailure):
            parser.parse(Response(200, {'Content-Type': 'application/json'}, '{"message": "error"}', url))

    def test_post_number_is_list_item_id_like_original(self):
        expected, _ = self.native()
        self.assertEqual(['900001', '900000'], [p['external_id'] for p in expected])


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{SITE: (200, 'application/json', EMPTY)})


if __name__ == '__main__':
    unittest.main()
