"""rfi-ac-uk-latest split: WP REST news posts (category 8), one row -> crawl's paper. Memory only."""
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from crawler.enumeration.http import PermanentFailure
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load

SITE = 'rfi-ac-uk-latest'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        captured, calls = [], []

        def api_get(url):
            calls.append(url)
            if '&page=1&' in url:
                return fixture['listing_body']
            return json.dumps({'code': 'rest_post_invalid_page_number', 'data': {'status': 400}})
        crawler._api_get = api_get
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        return captured, calls

    def test_listing_error_object_is_a_parse_error(self):
        crawler, _ = load(self.SITE)
        with self.assertRaises(PermanentFailure):
            crawler._enumeration_items({'code': 'rest_no_route'})


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
