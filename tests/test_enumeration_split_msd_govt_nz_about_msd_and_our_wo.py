"""L3b-20261018: enumeration split for msd-govt-nz-about-msd-and-our-wo.
The single Regulatory Impact Statements page (no pagination; crawl() reads one
page). Direct PDF/Word rows are left out as crawl() skips them; HTML rows keep
crawl()'s item_index. Page numbers above 1 are declared empty and proved by
the listing_marker (div.block)."""
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from crawler.enumeration.urls import build_url
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split

SITE = 'msd-govt-nz-about-msd-and-our-wo'
MAINTENANCE = '<html><body><p>Site under maintenance</p></body></html>'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        captured, calls = [], []
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))

        def fetch(url):
            calls.append(url)
            return fixture['details'].get(url) if url in fixture['details'] else fixture['listing_body']
        crawler._fetch_html = fetch
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        return captured, calls

    def test_full_paper_and_identity_equal_original_crawl(self):
        super().test_full_paper_and_identity_equal_original_crawl()
        self.assertEqual(['synthetic-ris-two', 'synthetic-sar-one'], [p['external_id'] for p in self.raw_papers])
        self.assertEqual([1, 3], [json.loads(p['metadata'])['item_index'] for p in self.raw_papers])

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
        for url in (p.listing['url'], build_url(p.listing['url'], {'page': 1})):
            with self.assertRaises(PermanentFailure):
                parser.parse(Response(200, {'Content-Type': 'text/html'}, MAINTENANCE, url))


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def test_shipped_plan_completes_end_to_end(self):
        same_page = lambda url: Response(200, {'Content-Type': 'text/html'}, self.fixture['listing_body'], url)
        result, docs = run_split(self.SITE, end=same_page)
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(2, len(docs))


if __name__ == '__main__':
    unittest.main()
