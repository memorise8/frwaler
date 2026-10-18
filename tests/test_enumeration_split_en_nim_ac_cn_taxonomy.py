"""en-nim-ac-cn-taxonomy: split (_enumeration_items / _enumeration_paper) must produce
exactly the papers crawl() saves; crawl() is unchanged."""
import unittest
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split

SITE = 'en-nim-ac-cn-taxonomy'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        # crawl() reads self.detail_delay, normally set by __init__ (not run by the helper).
        import tests.collector_split_helpers as helpers
        original = helpers.load

        def load_with_delay(site):
            crawler, module = original(site)
            crawler.detail_delay = 0
            return crawler, module
        with patch.object(helpers, 'load', load_with_delay):
            return super().native(fixture)

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?page=31'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['empty_body'], url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'},
                                           '<html><body><main>maintenance</main></body></html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))

    def test_identity_is_paper_id(self):
        expected, _ = self.native()
        self.assertEqual(['222', '201'], [p['external_id'] for p in expected])


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def test_shipped_plan_completes_end_to_end(self):
        body = self.fixture['empty_body']
        result, docs = run_split(SITE, end=lambda url: Response(200, {'Content-Type': 'text/html'}, body, url))
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(['201', '222'], sorted(d['post_number'] for d in docs))


if __name__ == '__main__':
    unittest.main()
