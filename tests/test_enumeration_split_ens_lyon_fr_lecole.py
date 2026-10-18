"""ens-lyon-fr-lecole: split (_enumeration_items / _enumeration_paper) must produce exactly
the papers the listing crawl (_crawl_listing) saves; crawl() is unchanged."""
import unittest

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split

SITE = 'ens-lyon-fr-lecole'
# Shape of the real page past the last one (?page=20, 2026-10-18): the view, no cards.
EMPTY_PAGE = ('<html><body><main><div class="views-element-container"><div class="view view-communiques-de-presse '
              'view-id-communiques_de_presse view-display-id-page_1"><nav class="pager"></nav></div></div></main></body></html>')


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        # The registered listing crawl; crawl() itself dispatches on delivery_mode.
        import tests.collector_split_helpers as helpers
        from unittest.mock import patch
        original = helpers.load

        def load_listing(site):
            crawler, module = original(site)
            crawler.delivery_mode = 'incremental'
            return crawler, module
        with patch.object(helpers, 'load', load_listing):
            return super().native(fixture)

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?page=20'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY_PAGE, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'},
                                           '<html><body><div class="views-element-container"></div></body></html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))

    def test_identity_is_drupal_node_id(self):
        expected, _ = self.native()
        self.assertEqual(['1633732', '1581627'], [p['post_number'] for p in expected])

    def test_failed_detail_raises_instead_of_silent_skip(self):
        crawler, _ = load(SITE)
        with self.assertRaises(ValueError):
            crawler._enumeration_paper('<html><body><main></main></body></html>',
                                       {'node_id': '1', 'url': 'https://www.ens-lyon.fr/x'})


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def test_shipped_plan_completes_end_to_end(self):
        result, docs = run_split(SITE, end=lambda url: Response(200, {'Content-Type': 'text/html'}, EMPTY_PAGE, url))
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))


if __name__ == '__main__':
    unittest.main()
