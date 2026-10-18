"""L3b-20261018: enumeration split for educacionfpydeportes-gob-es-prensahtml.

List: GET /prensa/actualidad.html?texto=&desde=2000-01-01&hasta=2030-12-31 returns every
press release on one page; the site ignores ?page=, so page 2 repeats page 1 (wrap) and
last_page_backward ends at page 1. The crawler fetches through self._curl (subprocess);
the native run routes it to the fixture transport."""
import unittest
from unittest.mock import patch

import tests.collector_split_helpers as helpers
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, split_fixture

SITE = 'educacionfpydeportes-gob-es-prensahtml'


def _load_with_curl_routed(site):
    # load() executes the module afresh, so only this run's class is changed.
    crawler, module = load(site)
    type(crawler)._curl = lambda self, url, **kw: self._curl_get(url)
    return crawler, module


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        with patch.object(helpers, 'load', _load_with_curl_routed):
            captured, calls = super().native(fixture)
        # crawl() leaves site_id to BaseCrawler._save_paper (setdefault); the split sets it.
        for paper in captured:
            paper.setdefault('site_id', SITE)
        return captured, calls

    def test_valid_empty_page_and_error_page_are_distinct(self):
        # The listing never ends on an empty page (one page, then wrap), so the plan has
        # no listing_marker: any page without rows is a listing error, never an end.
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.http import Response
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?texto=&desde=2000-01-01&hasta=2030-12-31&page=1'
        for body in ('<html><main/></html>', '<html>maintenance</html>'):
            page = parser.parse(Response(200, {'Content-Type': 'text/html'}, body, url))
            self.assertEqual([], page.items)
            self.assertTrue(page.extra.get('parse_error'))
            self.assertFalse(page.extra.get('explicit_empty', False))

    def test_post_number_is_url_slug_like_original(self):
        expected, _ = self.native()
        self.assertEqual(['20260211-sintetica-uno', '20260110-sintetica-cero'], [p['external_id'] for p in expected])


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    # Past page 1 the site serves page 1 again (wrap).
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{SITE: (200, 'text/html', split_fixture(SITE)['listing_body'])})


if __name__ == '__main__':
    unittest.main()
