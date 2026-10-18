"""L3b-20261018: enumeration split for minedu-gov-gr-grafeio-typoy-kai-di.
Joomla list ?start=N (offset, 20 rows); past the end the server clamps to the
last page (the framework's wrap/clamp rule ends the listing). Detail conversion
of _process_article() copied verbatim (external_id = Joomla id from the URL)."""
import unittest

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load

SITE = 'minedu-gov-gr-grafeio-typoy-kai-di'
EMPTY = '<html><body><div class="custom-article-list"></div></body></html>'
MAINTENANCE = '<html><body><p>Site under maintenance</p></body></html>'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_full_paper_and_identity_equal_original_crawl(self):
        super().test_full_paper_and_identity_equal_original_crawl()
        self.assertEqual(['90002', '90001'], [p['external_id'] for p in self.raw_papers])
        self.assertEqual(['2031-02-13', '2031-01-09'], [p['published_date'] for p in self.raw_papers])

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?start=40'
        empty = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY, url))
        self.assertEqual([], empty.items)
        self.assertTrue(empty.extra.get('explicit_empty'))
        with self.assertRaises(PermanentFailure) as ctx:
            parser.parse(Response(200, {'Content-Type': 'text/html'}, MAINTENANCE, url))
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{SITE: (200, 'text/html', EMPTY)})


if __name__ == '__main__':
    unittest.main()
