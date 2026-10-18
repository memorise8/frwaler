"""L3b-20261018: enumeration split for nivel-nl-en.
crawl() reads the Drupal publications view (start page, then views/ajax?page=N);
the same view answers a plain GET ?page=N (0-based, 30 rows). Rows are parsed
with _parse_list_items and papers built with _build_paper as crawl() does
(external_id = Drupal node id, else slug). A page past the end keeps
.view-publications without rows (listing_marker)."""
import unittest

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load

SITE = 'nivel-nl-en'
EMPTY = '<html><body><div class="view-publications"><div class="view-empty"></div></div></body></html>'
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
        url = p.listing['url'] + '?page=181'
        empty = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY, url))
        self.assertEqual([], empty.items)
        self.assertTrue(empty.extra.get('explicit_empty'))
        foreign = parser.parse(Response(200, {'Content-Type': 'text/html'}, MAINTENANCE, url))
        self.assertTrue(foreign.extra.get('parse_error'))
        self.assertFalse(foreign.extra.get('explicit_empty', False))


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{SITE: (200, 'text/html', EMPTY)})


if __name__ == '__main__':
    unittest.main()
