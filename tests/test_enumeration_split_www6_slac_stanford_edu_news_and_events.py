"""www6-slac-stanford-edu-news-and-events: split (_enumeration_items / _enumeration_detail /
_enumeration_paper) must produce exactly the papers crawl() saves; crawl() is unchanged and
post_number stays the Drupal data-history-node-id."""
import unittest

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split

SITE = 'www6-slac-stanford-edu-news-and-events'
# Shape of the real page past the last one (archive ?page=28, 2026-10-18): view wrapper and
# exposed filter form, no c-view__row.
EMPTY_PAGE = ('<html><body><main><div class="c-view c-view--landing-pages c-view--display-news_archive view">'
              '<div class="c-view__exposed"><form class="views-exposed-form bef-exposed-form"></form></div>'
              '</div></main></body></html>')


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?page=28'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY_PAGE, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'},
                                           '<html><body><main>maintenance</main></body></html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))

    def test_identity_is_node_id_and_href_is_stripped(self):
        expected, _ = self.native()
        self.assertEqual(['6247', '6253'], [p['external_id'] for p in expected])
        self.assertTrue(all(not p['url'].endswith(' ') for p in expected))


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
