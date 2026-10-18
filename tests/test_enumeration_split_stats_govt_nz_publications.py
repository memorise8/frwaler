"""stats-govt-nz-publications: split (_enumeration_items / _enumeration_paper) must produce
exactly the papers crawl()'s direct path saves (listing row first, detail pageViewData only
when the listing abstract is short); crawl() is unchanged and external_id stays the page ID."""
import unittest

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split

SITE = 'stats-govt-nz-publications'
# Shape of the real page past the last one (start=228, 2026-10-18): pageViewData, no rows.
EMPTY_PAGE = ('<html><body><div id="pageViewData" data-value="{&quot;PaginatedBlockPages&quot;: [], '
              '&quot;totalPages&quot;: 19}"></div></body></html>')


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        import tests.collector_split_helpers as helpers
        from unittest.mock import patch
        original = helpers.load

        def load_with_stubs(site):
            crawler, module = original(site)
            crawler._report_source_failure = lambda *a, **k: None
            return crawler, module
        with patch.object(helpers, 'load', load_with_stubs):
            return super().native(fixture)

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?categoryFiltersID=385&filters=Reports&start=228'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY_PAGE, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'},
                                           '<html><body><main>maintenance</main></body></html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))

    def test_identity_is_page_id(self):
        expected, _ = self.native()
        self.assertEqual(['80677', '79966'], [p['external_id'] for p in expected])
        self.assertEqual('', expected[0]['category'])           # listing-row paper
        self.assertEqual('Population', expected[1]['category'])  # detail paper


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
