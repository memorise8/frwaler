import unittest
from unittest.mock import patch

from tests import collector_split_helpers as helpers
from tests.collector_split_helpers import SplitChecks, SplitRunChecks

SITE = 'dhs-gov-news-releases'
EMPTY = '<html><body><div class="view view-news-events"><div class="view-empty">No results</div></div></body></html>'


def _load_with_plain_fetch(site, _load=helpers.load):
    """crawl() fetches through module-level _fetch (curl_cffi/Playwright); route it
    through _curl_get so the shared memory-only harness can serve the fixture."""
    crawler, module = _load(site)
    if site == SITE:
        module._curl_get = lambda url, *a, **k: None
        module._fetch = lambda url, *a, **k: module._curl_get(url)
    return crawler, module


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def setUp(self):
        super().setUp()
        p = patch.object(helpers, 'load', _load_with_plain_fetch); p.start(); self.addCleanup(p.stop)

    def test_valid_empty_page_and_error_page_are_distinct(self):
        # The real past-the-end page keeps the news view with a .view-empty block.
        from urllib.parse import urlencode
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.http import Response
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = helpers.load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?' + urlencode({'page': 300})
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_PAGES = {**SplitRunChecks.END_PAGES, SITE: (200, 'text/html', EMPTY)}
