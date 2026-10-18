"""L3b-20261018: enumeration split for hi-no-en (GET /en/hi/nettrapporter?query=&start=N, offset 30/page).
Past the last page the site answers 200 with the report list frame (h2.padd2) and no items."""
import unittest

from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, split_fixture

SITE = 'hi-no-en'
EMPTY = split_fixture(SITE)['empty_body']


def _with_site(papers):
    # crawl() leaves site_id out; BaseCrawler._save_paper and the adapter both
    # setdefault it to the same value, so the stored document is identical.
    from crawler.enumeration.adapters import clean_paper
    return [dict(clean_paper(p), site_id=p.get('site_id', SITE)) for p in papers]


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_full_paper_and_identity_equal_original_crawl(self):
        from crawler.enumeration.adapters import paper_identity
        expected, _ = self.native()
        self.assertEqual(2, len(expected), 'fixture must exercise two actual saved papers')
        self.assertNotIn('site_id', expected[0])
        actual = self.pipeline()
        self.assertEqual(expected, self.raw_papers, 'raw fields must match before adapter cleaning')
        self.assertEqual(_with_site(expected), actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])
        self.assertEqual(2, len({paper_identity(p) for p in actual}))

    def test_detail_redirect_preserves_registered_identity(self):
        expected, _ = self.native()
        self.assertEqual(_with_site(expected), self.pipeline(redirect=True))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.http import Response
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?query=&start=600'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))

    def test_post_number_is_sqlid_or_slug_like_original(self):
        expected, _ = self.native()
        self.assertEqual(['109001', 'synthetic-report-9000'], [p['external_id'] for p in expected])
        # posted_date comes from the listing row, published_date falls back to it
        self.assertEqual('2026-09-11', expected[1]['published_date'])

    def test_offset_paging_in_shipped_plan(self):
        listing = self.plan['sites'][SITE]['listing']
        self.assertEqual(('start', 0, 'offset', 30), (listing['page_param'], listing['start_page'],
                                                       listing['page_mode'], listing['page_size']))


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{SITE: (200, 'text/html', EMPTY)})


if __name__ == '__main__':
    unittest.main()
