"""em-lyon-com-en: split (_enumeration_items / _enumeration_paper) must produce exactly
the papers crawl() saves; crawl() is unchanged."""
import unittest

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split

SITE = 'em-lyon-com-en'
# Shape of the real page past the last one (?page=86, 2026-10-18): empty view container.
EMPTY_PAGE = '<html><body class="path-newsroom"><main><div class="views-element-container"></div></main></body></html>'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?page=86'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY_PAGE, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html><body><main>maintenance</main></body></html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))

    def test_identity_is_drupal_node_id(self):
        expected, _ = self.native()
        self.assertEqual(['38251', '38288'], [p['post_number'] for p in expected])


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
