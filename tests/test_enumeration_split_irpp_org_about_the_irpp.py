"""L3b-20261018: enumeration split for irpp-org-about-the-irpp.
The single annual-reports page (no pagination; crawl() reads one page). Page
numbers above 1 are declared empty and proved by the listing_marker
(div.annual-report); cards become papers with the pure copy of crawl()'s
conversion. crawl() extracts the abstract from the PDF with curl + pdftotext;
the pure method stores crawl()'s own fallback abstract instead, so the
equality check runs crawl() with PDF extraction failing."""
import contextlib
import copy
import io
import unittest
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from crawler.enumeration.urls import build_url
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split

SITE = 'irpp-org-about-the-irpp'
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
        crawler._curl_get = lambda url: calls.append(url) or fixture['listing_body']
        crawler._extract_abstract_from_pdf = lambda pdf_url: None
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertEqual(1, len(calls))
        return captured, calls

    def test_full_paper_and_identity_equal_original_crawl(self):
        super().test_full_paper_and_identity_equal_original_crawl()
        self.assertEqual(['2031', '2030'], [p['post_number'] for p in self.raw_papers])
        self.assertEqual(['Synthetic-Annual-Report-2031', 'Synthetic-Annual-Report-2030'],
                         [p['external_id'] for p in self.raw_papers])
        self.assertTrue(all(p['url'] == 'https://irpp.org/about-the-irpp/annual-reports/' for p in self.raw_papers))

    def test_short_abstract_skip_is_preserved(self):
        # The fallback abstract is a fixed template (>= 50 chars): crawl() never skips a card for it.
        expected, _ = self.native()
        self.assertEqual(2, len(expected))
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline())

    def _parser(self):
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        return p, ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p, parser = self._parser()
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
