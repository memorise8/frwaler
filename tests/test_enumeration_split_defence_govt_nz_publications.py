"""L3b-20261018: enumeration split for defence-govt-nz-publications (GET
/publications/doSearch/?start=N, item offset, 10 rows). Past the last page the site
answers 200 with an empty div.search-page__results (2026-10-18: start=450)."""
import unittest

from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load

SITE = 'defence-govt-nz-publications'
EMPTY = ('<html><body><div class="search-page__results"><div class="search-results__info">0 results</div>'
         '</div></body></html>')


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_valid_empty_page_and_error_page_are_distinct(self):
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.http import Response
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?start=450'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))

    def test_post_number_is_slug_like_original(self):
        expected, _ = self.native()
        self.assertEqual(['synthetic-publication-b', 'synthetic-publication-a'], [p['external_id'] for p in expected])
        self.assertEqual(['2026-02-11', '2026-01-10'], [p['published_date'] for p in expected])


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{SITE: (200, 'text/html', EMPTY)})


if __name__ == '__main__':
    unittest.main()
