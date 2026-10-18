"""L3b-20261018: enumeration split for hea-ie-resources (WordPress /resources/publications/?paged=N,
301 to /page/N/, 12 cards per page; past the last page the site answers 404).
The detail request is the card's PDF: crawl() takes the abstract from the PDF text (pypdf,
in-process). Cards without a PDF link are left out (crawl() always skips them)."""
import unittest

from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, split_fixture

SITE = 'hea-ie-resources'
EMPTY = split_fixture(SITE)['empty_body']


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_valid_empty_page_and_error_page_are_distinct(self):
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.http import Response
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?paged=30'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))

    def test_post_number_is_pdf_filename_like_original(self):
        expected, _ = self.native()
        self.assertEqual(['Synthetic-Report-9001', 'Synthetic-Report-9000'], [p['post_number'] for p in expected])
        self.assertEqual('https://hea.ie/resources/publications/synthetic-publication-9001/', expected[0]['url'])

    def test_card_without_pdf_is_not_listed(self):
        crawler, _ = load(SITE)
        rows = crawler._enumeration_items(self.fixture['listing_body'])
        self.assertEqual(2, len(rows))
        self.assertTrue(all(r['url'].endswith('.pdf') for r in rows))


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{SITE: (404, 'text/html', '<html><body>Not found</body></html>')})


if __name__ == '__main__':
    unittest.main()
