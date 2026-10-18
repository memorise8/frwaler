"""L3b-20261018: enumeration split for deltares-nl-en (Nuxt SSR GET
/en/expertise/publications?languages=Dutch&languages=English&materials=ARTICLE&page=N,
12 rows; 2026-10-18: 181 results, last page 16). Past the last page the overview grid is
served without rows. The NUXT pdf URL keeps crawl()'s \\u002F escapes (unchanged behaviour)."""
import unittest

from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load

SITE = 'deltares-nl-en'
EMPTY = '<html><body><div class="grid grid-cols-12 gap-8 v-grid-template-rows--overview"><ol></ol></div></body></html>'


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
        url = p.listing['url'] + '&page=17'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))

    def test_post_number_is_nuxt_publication_id_like_original(self):
        expected, _ = self.native()
        self.assertEqual(['990002', '990001'], [p['external_id'] for p in expected])
        self.assertEqual(['990002', '990001'], [p['post_number'] for p in expected])
        self.assertEqual('S. van Bakker; T. Tester', expected[0]['authors'])


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{SITE: (200, 'text/html', EMPTY)})


if __name__ == '__main__':
    unittest.main()
