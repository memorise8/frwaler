"""issnationallab-org-about: split (_enumeration_items / _enumeration_detail / _enumeration_paper)
must produce exactly the papers crawl() saves from the single report page; crawl() is unchanged
and post_number stays fy##-q# / fy##-annual-report / arYYYY. The page has no paging: ?page=2
serves the same page, which the framework ends as a wrap (last page = 1)."""
import unittest

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split, split_fixture

SITE = 'issnationallab-org-about'
EMPTY_PAGE = '<html><body><main><div class="page-content"><p>No reports.</p></div></main></body></html>'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?page=1'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY_PAGE, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))

    def test_identity_and_pdf_entries_never_fetch_the_pdf(self):
        expected, _ = self.native()
        self.assertEqual(['fy25-annual-report', 'fy25-q1'], [p['external_id'] for p in expected])
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        items = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler)).parse(
            Response(200, {'Content-Type': 'text/html'}, self.fixture['listing_body'], p.listing['url'])).items
        self.assertTrue(all('/download/' not in i.url for i in items))


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def test_shipped_plan_completes_end_to_end(self):
        body = split_fixture(SITE)['listing_body']
        # The real site answers ?page=2 with the same page: a wrap, so page 1 is the last page.
        result, docs = run_split(SITE, end=lambda url: Response(200, {'Content-Type': 'text/html'}, body, url))
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))
        self.assertEqual(2, len({d['meta_url'] for d in docs}))


if __name__ == '__main__':
    unittest.main()
