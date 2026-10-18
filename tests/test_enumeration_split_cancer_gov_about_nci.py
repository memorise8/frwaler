"""L3b-20261018: enumeration split for cancer-gov-about-nci — the single budget landing page
(crawl() reads one page; ?page=N returns the same page, so the runner sees a wrap at page 2).
Site rules kept from crawl(): every paper's url (meta_url) is the landing URL, post_number is
the PDF file slug, published_date stays empty (a fiscal year is a coverage period)."""
import json
import unittest

from crawler.enumeration.adapters import clean_paper
from crawler.enumeration.http import Response
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split, split_fixture

SITE = 'cancer-gov-about-nci'
FIXTURE = split_fixture(SITE)
LANDING = 'https://www.cancer.gov/about-nci/budget/about-budget-proposal'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_full_paper_and_identity_equal_original_crawl(self):
        super().test_full_paper_and_identity_equal_original_crawl()
        self.assertEqual(['fy-2031-annual-plan', '2030-annual-plan'], [p['external_id'] for p in self.raw_papers])
        self.assertEqual({LANDING}, {p['url'] for p in self.raw_papers})

    def test_short_abstract_skip_is_preserved(self):
        # The abstract always contains the fixed intro text: no entry is ever short.
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        expected, _ = self.native(f)
        self.assertEqual(2, len(expected))
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(f))

    def test_repeated_pdf_link_is_dropped_like_crawl(self):
        f = dict(self.fixture, listing_body=self.fixture['duplicate_body'])
        expected, _ = self.native(f)
        self.assertEqual(2, len(expected))
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(f))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html><main id="main-content"></main></html>', LANDING))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', LANDING))
        self.assertTrue(error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def test_shipped_plan_completes_end_to_end(self):
        same_page = lambda url: Response(200, {'Content-Type': 'text/html'}, FIXTURE['listing_body'], url)
        result, docs = run_split(self.SITE, end=same_page)
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual({'fy-2031-annual-plan', '2030-annual-plan'}, {d['post_number'] for d in docs})
        self.assertEqual({LANDING}, {d['meta_url'] for d in docs})


if __name__ == '__main__':
    unittest.main()
