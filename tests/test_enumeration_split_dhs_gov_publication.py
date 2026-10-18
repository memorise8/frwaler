import copy
import unittest
from unittest.mock import patch

from tests import collector_split_helpers as helpers
from tests.collector_split_helpers import SplitChecks, SplitRunChecks

SITE = 'dhs-gov-publication'


def _load_with_plain_fetch(site, _load=helpers.load):
    """crawl() fetches through module-level _fetch_playwright; route it through
    _curl_get so the shared memory-only harness can serve the fixture."""
    crawler, module = _load(site)
    if site == SITE:
        module._curl_get = lambda url, *a, **k: None
        module._fetch_playwright = lambda url, *a, **k: module._curl_get(url)
    return crawler, module


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def setUp(self):
        super().setUp()
        p = patch.object(helpers, 'load', _load_with_plain_fetch); p.start(); self.addCleanup(p.stop)

    def test_short_abstract_skip_is_preserved(self):
        # The abstract is built from a fixed page description (always long enough);
        # crawl()'s remaining item filter is the empty-title skip, checked instead.
        f = copy.deepcopy(self.fixture)
        f['listing_body'] = f['listing_body'].replace('FY 2021 Annual Report on Conferences', '').replace(
            'FY 2022 Annual Report on Conferences', '')
        expected, _ = self.native(f)
        self.assertEqual([], expected)
        self.assertEqual([], self.pipeline(f))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.http import Response
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = helpers.load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?page=0'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html><table></table></html>', url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    # The node page ignores ?page=N and repeats itself (wrap): page 0 is the last page.
    END_PAGES = {**SplitRunChecks.END_PAGES,
                 SITE: (200, 'text/html', helpers.split_fixture(SITE)['listing_body'])}

    def test_shipped_plan_completes_end_to_end(self):
        # crawl() stores every row with url = the listing page, so all rows share one
        # meta_url and differ by post_number (external_id = PDF file name); the run keeps that rule.
        from crawler.enumeration.http import Response
        body = self.fixture['listing_body']
        result, docs = helpers.run_split(SITE, end=lambda url: Response(200, {'Content-Type': 'text/html'}, body, url))
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual({'fy2021_conf', 'fy2022_conf'}, {d['post_number'] for d in docs})
        self.assertEqual({'https://www.dhs.gov/publication/annual-report-conferences'}, {d['meta_url'] for d in docs})
