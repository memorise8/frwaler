import unittest
from unittest.mock import patch

from tests import collector_split_helpers as helpers
from tests.collector_split_helpers import SplitChecks, SplitRunChecks

SITE = 'artsetmetiers-fr-fr'
EMPTY = ('<html><body><div class="view view-espace-presse"></div><div class="view view-partners-footer">'
         '<div class="views-row"><a href="https://partner.example/">Partner</a></div></div></body></html>')


def _load_with_plain_fetch(site, _load=helpers.load):
    """crawl() fetches list pages through _fetch_page_html (curl); route it through
    _curl_get so the shared memory-only harness can serve the fixture."""
    crawler, module = _load(site)
    if site == SITE:
        crawler._fetch_page_html = lambda n: crawler._curl_get(f'{crawler._LIST_URL}?page={n}')
    return crawler, module


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def setUp(self):
        super().setUp()
        p = patch.object(helpers, 'load', _load_with_plain_fetch); p.start(); self.addCleanup(p.stop)

    def test_valid_empty_page_and_error_page_are_distinct(self):
        # Past the end the press view is empty while footer views keep their rows.
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.http import PermanentFailure, Response
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = helpers.load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?page=60'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        with self.assertRaises(PermanentFailure):
            parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_PAGES = {**SplitRunChecks.END_PAGES, SITE: (200, 'text/html', EMPTY)}
