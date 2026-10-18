"""L3b-20261018: enumeration split for kefm-dk-aftaler.
The single agreements page (no pagination; crawl() reads one page and builds
every record from the list rows). Page numbers above 1 are declared empty and
proved by the listing_marker (div.module); the list row carries the record
crawl() would save (paper_method, no detail request)."""
import unittest

from crawler.enumeration.adapters import Adapter, clean_paper
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from crawler.enumeration.urls import build_url
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split

SITE = 'kefm-dk-aftaler'
MAINTENANCE = '<html><body><p>Site under maintenance</p></body></html>'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_full_paper_and_identity_equal_original_crawl(self):
        super().test_full_paper_and_identity_equal_original_crawl()
        # itemid first, then the /Media/<id>/ number (crawl()'s rule).
        self.assertEqual(['90001', '639000000000000002'], [p['external_id'] for p in self.raw_papers])
        self.assertEqual(['2031-02', '2030'], [p['published_date'] for p in self.raw_papers])

    def test_short_abstract_skip_is_preserved(self):
        # The abstract is a template with a fixed suffix (>= 50 chars): crawl() never skips a row for it.
        expected, _ = self.native()
        self.assertEqual(2, len(expected))
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline())

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        page2 = build_url(p.listing['url'], {'page': 2})
        empty = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['listing_body'], page2))
        self.assertEqual([], empty.items)
        self.assertTrue(empty.extra.get('explicit_empty'))
        foreign = parser.parse(Response(200, {'Content-Type': 'text/html'}, MAINTENANCE, page2))
        self.assertEqual('listing_structure_missing', foreign.extra.get('parse_error'))
        self.assertFalse(foreign.extra.get('explicit_empty', False))
        for url in (p.listing['url'], build_url(p.listing['url'], {'page': 1})):
            with self.assertRaises(PermanentFailure) as ctx:
                parser.parse(Response(200, {'Content-Type': 'text/html'}, MAINTENANCE, url))
            self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def test_shipped_plan_completes_end_to_end(self):
        same_page = lambda url: Response(200, {'Content-Type': 'text/html'}, self.fixture['listing_body'], url)
        result, docs = run_split(self.SITE, end=same_page)
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))


if __name__ == '__main__':
    unittest.main()
