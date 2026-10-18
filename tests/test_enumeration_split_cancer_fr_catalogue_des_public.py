"""cancer-fr-catalogue-des-public: last_page_backward plan over the filtered HTML catalogue
(collections 89 and 101, ``?page=N``) + pure conversions (memory only).

``_enumeration_items(raw, url)`` reuses the registered list parser and
``_enumeration_paper(raw, item)`` is a pure copy of ``_fetch_detail_record`` after its
fetch; both must give what the unchanged crawl() saves. The Algolia rescue phase of
crawl() (only used when the HTML list yields nothing) is not part of the plan.
"""
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from crawler.enumeration.http import PermanentFailure, Response
from tests.collector_split_helpers import load, run_split
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks

SITE = 'cancer-fr-catalogue-des-public'


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        captured, calls, listing_calls = [], [], []

        def transport(url, method='GET', body=None, headers=None, timeout=35):
            calls.append(url)
            if method.upper() == 'POST':
                return None  # Algolia rescue: unavailable offline
            if url in fixture['details']:
                return fixture['details'][url].encode('utf-8')
            listing_calls.append(url)
            return (fixture['listing_body'] if len(listing_calls) == 1 else fixture['empty_body']).encode('utf-8')
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._check_cancelled = lambda: None
        crawler._source_failure = None
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl = transport
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertLessEqual(len(listing_calls), 2)
        return captured, calls

    def test_added_listing_rejects_wrong_shape(self):
        crawler, _ = load(self.SITE)
        url = self.plan['sites'][self.SITE]['listing']['url'] + '&page=1'
        for value in ({'error': 'not a listing'}, b'<html><body>maintenance</body></html>'):
            with self.subTest(value=value):
                with self.assertRaises(PermanentFailure) as ctx:
                    crawler._enumeration_items(value, url)
                self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_identity_is_the_registered_node_id(self):
        expected, _ = self.native()
        self.assertEqual(['551', '552'], [p['external_id'] for p in expected])
        self.assertEqual(['551', '552'], [p['external_id'] for p in self.pipeline()])

    def test_list_page_url_metadata_on_page_one_matches_crawl(self):
        expected, _ = self.native()
        actual = self.pipeline()
        self.assertEqual([json.loads(p['metadata'])['list_page_url'] for p in expected],
                         [json.loads(p['metadata'])['list_page_url'] for p in actual])


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = SITE

    def test_clamped_last_page_completes(self):
        body = self.fixture['listing_body']
        result, docs = run_split(self.SITE, end=lambda url: Response(200, {'Content-Type': 'text/html'}, body, url))
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(2, len(docs))
