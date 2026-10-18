"""Site-specific glue for the L2b g4 enumeration splits (offline, memory-only).

The generic ``SplitChecks`` drive ``crawl()`` through ``_curl_get`` / ``_session.get``
and assume ``<html><main/></html>`` is a valid empty HTML listing. The g4 crawlers
reach the list through other helpers (``_curl`` with params, a POST
``_fetch_list_page``) and their plans name a site-specific ``listing_marker``.
These mixins route those helpers to the same fake transport and use the
fixture's real-shape empty page (``empty_body``). No network, process or DB.
"""
from unittest.mock import patch
from urllib.parse import urlencode

import tests.collector_split_helpers as base
from crawler.enumeration.adapters import Adapter
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan


def _route_list_helpers(crawler):
    """Send crawl's own list helpers to the transport the generic native() installs."""
    if hasattr(type(crawler), '_curl'):
        def curl(url, params=None, retries=3):
            if params:
                url = url + ('&' if '?' in url else '?') + urlencode(params)
            return crawler._curl_get(url)
        crawler._curl = curl
    if hasattr(type(crawler), '_fetch_list_page'):
        crawler._fetch_list_page = lambda page_num: crawler._curl_get(f'list-page:{page_num}')


class G4SplitChecks(base.SplitChecks):
    def native(self, fixture=None):
        fixture = fixture or self.fixture
        original_load = base.load

        def load(site):
            crawler, module = original_load(site)
            _route_list_helpers(crawler)
            return crawler, module

        # The generic native() answers crawl's second list request with an empty page,
        # which every g4 crawl() reads as "no items: stop".
        with patch.object(base, 'load', load):
            return super().native(fixture)

    def _parser(self):
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = base.load(self.SITE)
        adapter = Adapter(self.SITE, p.section('adapter'), None, crawler=crawler)
        url = p.listing['url'] + ('&' if '?' in p.listing['url'] else '?') + urlencode(p.listing.get('params', {}))
        return ListingParser(p.listing['parser'], adapter=adapter), url, crawler

    def test_added_listing_rejects_wrong_shape(self):
        parser, url, crawler = self._parser()
        bad = ({'error': 'not a listing'} if self.fixture['content_type'] == 'application/json'
               else '<html><body>error</body></html>')
        try:
            with guarded():
                rows = crawler._enumeration_items(bad, url)
        except PermanentFailure as exc:
            self.assertTrue(exc.reason.startswith('listing_parse_error:'), exc.reason)
        else:
            self.assertEqual([], rows)
            self.assertTrue(self.plan['sites'][self.SITE]['listing']['parser'].get('listing_marker'))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        parser, url, _ = self._parser()
        ctype = self.fixture['content_type']
        page = parser.parse(Response(200, {'Content-Type': ctype}, self.fixture['empty_body'], url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        self.assertNotIn('parse_error', page.extra)
        try:
            error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        except PermanentFailure as exc:
            self.assertTrue(exc.reason.startswith('listing_parse_error:'), exc.reason)
        else:
            self.assertTrue(error_page.extra.get('parse_error'))
            self.assertFalse(error_page.extra.get('explicit_empty', False))


class G4SplitRunChecks(base.SplitRunChecks):
    """End-to-end run of the shipped plan; the page after the last one is the
    fixture's real-shape end answer (empty HTML page, or WordPress 400)."""

    def test_shipped_plan_completes_end_to_end(self):
        f = self.fixture
        if f['content_type'] == 'application/json':
            end = base.wp_past_last_page
        else:
            end = lambda url: Response(200, {'Content-Type': 'text/html'}, f['empty_body'], url)
        result, docs = base.run_split(self.SITE, end=end)
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))
        self.assertEqual(2, len({d['meta_url'] for d in docs}))
