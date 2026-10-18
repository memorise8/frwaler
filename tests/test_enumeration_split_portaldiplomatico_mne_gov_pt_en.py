"""L3b-20261018: enumeration split for portaldiplomatico-mne-gov-pt-en.
Joomla blog list ?start=N (offset, 6 rows); a page past the end keeps the
div.blog frame with no rows (listing_marker). Detail conversion copied verbatim
(external_id = post_number = URL slug)."""
import unittest

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load

SITE = 'portaldiplomatico-mne-gov-pt-en'
EMPTY = '<html><body><main><div class="blog"><p class="counter">Page 64 of 64</p></div></main></body></html>'
MAINTENANCE = '<html><body><p>Site under maintenance</p></body></html>'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_full_paper_and_identity_equal_original_crawl(self):
        super().test_full_paper_and_identity_equal_original_crawl()
        self.assertEqual(['synthetic-90002', 'synthetic-90001'], [p['external_id'] for p in self.raw_papers])
        self.assertEqual(['2031-02-13', '2031-01-09'], [p['published_date'] for p in self.raw_papers])

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?start=384'
        empty = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY, url))
        self.assertEqual([], empty.items)
        self.assertTrue(empty.extra.get('explicit_empty'))
        foreign = parser.parse(Response(200, {'Content-Type': 'text/html'}, MAINTENANCE, url))
        self.assertEqual([], foreign.items)
        self.assertTrue(foreign.extra.get('parse_error'))
        self.assertFalse(foreign.extra.get('explicit_empty', False))


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{SITE: (200, 'text/html', EMPTY)})


if __name__ == '__main__':
    unittest.main()
