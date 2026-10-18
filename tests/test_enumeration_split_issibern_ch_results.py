import copy
import json
import unittest
from unittest.mock import patch

from tests import collector_split_helpers as helpers
from tests.collector_split_helpers import SplitChecks, SplitRunChecks

SITE = 'issibern-ch-results'
LIST_PAGE = 'https://www.issibern.ch/results/publications/journal-publications/'
NONCE_PAGE = '<html><script>var FWP_JSON = {"nonce":"abc123def0"};</script></html>'
EMPTY = json.dumps({'template': '', 'settings': {'pager': {'total_pages': 1}}})


def _load_with_plain_post(site, _load=helpers.load):
    """crawl() reads the nonce with _curl_get and posts with _curl_post_json (curl); route the
    POST through _curl_get so the shared memory-only harness serves the fixture."""
    crawler, module = _load(site)
    if site == SITE:
        crawler._curl_post_json = lambda url, payload, nonce: json.loads(crawler._curl_get(url))
    return crawler, module


def _with_nonce_page(fixture):
    fixture = copy.deepcopy(fixture)
    fixture['details'][LIST_PAGE] = NONCE_PAGE
    return fixture


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def setUp(self):
        super().setUp()
        p = patch.object(helpers, 'load', _load_with_plain_post); p.start(); self.addCleanup(p.stop)

    def native(self, fixture=None):
        # crawl() first reads the public page nonce (the framework does the same via listing.dynamic).
        return super().native(_with_nonce_page(fixture or self.fixture))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.http import PermanentFailure, Response
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = helpers.load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        page = parser.parse(Response(200, {'Content-Type': 'application/json'}, EMPTY, p.listing['url']))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        for body in ('[]', '{"code": "rest_no_route"}'):
            with self.subTest(body=body), self.assertRaises(PermanentFailure):
                parser.parse(Response(200, {'Content-Type': 'application/json'}, body, p.listing['url']))


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def setUp(self):
        super().setUp()
        # The shared FakeTransport predates POST listings; accept (and ignore) the body here.
        from tests import enumeration_fakes
        original = enumeration_fakes.FakeTransport.request

        def request(transport, method, url, *, headers=None, data=None):
            return original(transport, method, url, headers=headers)
        p = patch.object(enumeration_fakes.FakeTransport, 'request', request); p.start(); self.addCleanup(p.stop)

    def test_shipped_plan_completes_end_to_end(self):
        from crawler.enumeration.http import Response
        fixture = _with_nonce_page(helpers.split_fixture(SITE))
        result, docs = helpers.run_split(SITE, fixture=fixture,
                                         end=lambda url: Response(200, {'Content-Type': 'application/json'}, EMPTY, url))
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))

    def test_error_listing_ends_incomplete_as_listing_parse_error(self):
        fixture = _with_nonce_page(helpers.split_fixture(SITE))
        for name, body in {'maintenance_page': '<html><body>maintenance</body></html>',
                           'json_error_object': json.dumps({'code': 'rest_no_route'})}.items():
            with self.subTest(case=name):
                result, docs = helpers.run_split(SITE, fixture=fixture, listing=body,
                                                 listing_type='application/json' if name.startswith('json') else 'text/html')
                self.assertEqual('incomplete', result['status'], result)
                self.assertEqual([], docs)
