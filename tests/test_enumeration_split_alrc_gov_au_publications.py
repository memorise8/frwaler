"""L3b-20261018: enumeration split for alrc-gov-au-publications, scope 'final-report'.
GET /publications/final-report/?paged=N (301 -> /page/N/, 10 rows); past the last
page the source answers 200 with an empty section.section-listing. crawl() lives in AlrcTraversal and is unchanged; the
reference conversion is the crawler's own _alrc_list()/_alrc_paper()."""
import copy
import json
import unittest
from unittest.mock import patch

from crawler.alrc_scope import list_url
from crawler.enumeration.adapters import Adapter
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load

SITE = 'alrc-gov-au-publications'
EMPTY = ('<html><body><div id="main-content"><main class="content"><h1>Publications</h1>'
         '<section class="section-listing posts"><div class="section-listing-inner"></div></section></main></div></body></html>')


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        """What crawl() saves for the final-report page: _alrc_list + _alrc_paper
        (the exact calls AlrcTraversal.crawl makes), fed from the fixture."""
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        calls = []

        def fetch(url):
            calls.append(url)
            if url in fixture['details']:
                return fixture['details'][url]
            self.assertEqual(list_url('final-report', 1), url)
            return fixture['listing_body']
        crawler._alrc_fetch = fetch
        items, last = crawler._alrc_list('final-report', 1)
        self.assertEqual(1, last)
        papers = []
        for item in items:
            paper = crawler._alrc_paper(item, 'final-report', list_url('final-report', 1))
            if paper is not None:
                papers.append(copy.deepcopy(paper))
        return papers, calls

    def test_post_number_is_wp_post_id_like_original(self):
        expected, _ = self.native()
        self.assertEqual(['90009', '90008'], [p['external_id'] for p in expected])

    def test_valid_empty_page_and_error_page_are_distinct(self):
        # Past the last page the source answers 200 with an empty section.section-listing.
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        url = 'https://www.alrc.gov.au/publications/final-report/page/13/'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertEqual('listing_structure_missing', error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))
        # Rows whose pager contradicts the requested page are a listing parse error.
        with self.assertRaises(PermanentFailure) as ctx:
            parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['listing_body'], url))
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_page_number_is_read_from_final_url(self):
        crawler, _ = load(self.SITE)
        body = self.fixture['listing_body']
        with self.assertRaises(PermanentFailure):  # pager says page 1, URL says page 2
            crawler._enumeration_items(body, 'https://www.alrc.gov.au/publications/final-report/page/2/')
        rows = crawler._enumeration_items(body, 'https://www.alrc.gov.au/publications/final-report/?paged=1')
        self.assertEqual({list_url('final-report', 1)}, {r['source_list_url'] for r in rows})


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{SITE: (200, 'text/html', EMPTY)})


if __name__ == '__main__':
    unittest.main()
