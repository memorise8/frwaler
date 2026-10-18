"""L3b-20261018: enumeration split for dia-govt-nz-diawebsitensf. One Domino listing page
(…Annual-Reports?OpenDocument, 28 annual reports on 2026-10-18); ?page=N is ignored, so page 2
repeats page 1 and the strategy ends there. Each entry is enriched from its detail page, else
its HTML-version page; pdf-only entries are not fetched (robots.txt disallows /*.pdf), the
runner re-reads the listing URL for them and the paper ignores that body, as crawl() does."""
import json
import unittest
from unittest.mock import patch

from tests import collector_split_helpers as helpers
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, split_fixture

SITE = 'dia-govt-nz-diawebsitensf'
LIST_URL = ('https://www.dia.govt.nz/diawebsite.nsf/wpg_URL/'
            'Resource-material-Corporate-Publications-Annual-Reports?OpenDocument=&page=1')


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        real_load = helpers.load

        def load_with_curl(site):
            crawler, module = real_load(site)
            crawler._curl = lambda url, *a, **k: crawler._curl_get(url)
            return crawler, module
        with patch.object(helpers, 'load', load_with_curl):
            return super().native(fixture)

    def test_short_abstract_skip_is_preserved(self):
        # crawl() never skips: a short description is padded with subject/body text or,
        # below 50 chars, replaced by the fixed annual-report text.
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        expected, _ = self.native(f)
        self.assertEqual(2, len(expected))
        self.assertTrue(expected[0]['abstract'].startswith('Short.'))
        self.assertEqual([helpers.clean_paper(p) for p in expected], self.pipeline(f))

    def test_added_listing_rejects_wrong_shape(self):
        from crawler.enumeration.guard import guarded
        from crawler.enumeration.http import PermanentFailure
        c, _ = load(SITE)
        with self.assertRaises(PermanentFailure) as ctx:
            with guarded():
                c._enumeration_items({'error': 'not a listing'}, LIST_URL)
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        # A single listing page is never legitimately empty: without rows it is a parse error.
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.http import Response
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        for body in ('<html><main/></html>', '<html>maintenance</html>'):
            page = parser.parse(Response(200, {'Content-Type': 'text/html'}, body, LIST_URL))
            self.assertEqual([], page.items)
            self.assertTrue(page.extra.get('parse_error'))
            self.assertFalse(page.extra.get('explicit_empty', False))

    def test_post_number_and_identity_like_original(self):
        expected, _ = self.native()
        self.assertEqual(['2024-25', '2023-24'], [p['external_id'] for p in expected])
        self.assertEqual(['2025', '2024'], [p['post_number'] for p in expected])
        self.assertEqual(['2025-10-20', '2024-10-21'], [p['published_date'] for p in expected])

    def test_pdf_only_entry_is_not_fetched_and_matches_crawl(self):
        listing = ('<html><body><h2>1996/97 Synthetic Annual Report</h2>'
                   '<p><a href="/diawebsite.nsf/Files/Syn-1996-97/$file/Syn-1996-97.pdf">PDF</a></p></body></html>')
        f = dict(self.fixture, listing_body=listing, details={})
        expected, calls = self.native(f)
        self.assertEqual(1, len(calls))                      # listing only
        crawler, _ = load(SITE)
        rows = crawler._enumeration_items(listing, LIST_URL)
        self.assertEqual(LIST_URL, rows[0]['fetch_url'])
        paper = crawler._enumeration_paper('<html>listing body is ignored</html>', rows[0])
        self.assertEqual(expected, [paper])
        self.assertEqual('1996-97', paper['external_id'])


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_PAGES = dict(SplitRunChecks.END_PAGES,
                     **{SITE: (200, 'text/html', split_fixture(SITE)['listing_body'])})


if __name__ == '__main__':
    unittest.main()
