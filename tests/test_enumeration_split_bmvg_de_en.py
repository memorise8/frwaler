"""L3b-20261018: enumeration split for bmvg-de-en (GET queryL-list-filter ?targetView=asFilterableList&offset=N,
6 rows per page, offset paging). Past the last offset the site answers 200 with an empty <template>;
the template's data-json carries totalItemCount."""
import unittest

from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split

SITE = 'bmvg-de-en'
EMPTY = "<template data-json='{\"limit\":6,\"totalItemCount\":2}'>\n</template>"


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_valid_empty_page_and_error_page_are_distinct(self):
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.http import Response
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], total_cfg=p.listing.get('total'),
                               adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?targetView=asFilterableList&offset=24'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        self.assertEqual(2, page.total)
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))

    def test_post_number_is_url_trailing_id_like_original(self):
        expected, _ = self.native()
        self.assertEqual(['9000002', '9000001'], [p['external_id'] for p in expected])

    def test_teaser_fallback_is_preserved(self):
        # No RichText on the detail page: crawl() falls back to the list teaser.
        import json
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False))
        for url in f['details']:
            f['details'][url] = f['details'][url].replace('class="RichText"', 'class="Other"')
        expected, _ = self.native(f)
        self.assertEqual(2, len(expected))
        self.assertEqual(expected, [dict(p) for p in self._raw(f)])

    def _raw(self, f):
        self.pipeline(f)
        return self.raw_papers


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{SITE: (200, 'text/html', EMPTY)})


if __name__ == '__main__':
    unittest.main()
