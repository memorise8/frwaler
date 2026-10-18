import contextlib
import copy
import io
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from tests.collector_split_helpers import load
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = 'nps-or-kr-inforls'

    def native(self, fixture=None):
        """Unchanged crawl() with an in-memory _curl_get(url, params) transport."""
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        captured = []; calls = []; listing_calls = []
        def transport(url, params=None):
            if params:
                url = url + '?' + urlencode(params)
            calls.append(url)
            if url in fixture['details']:
                return fixture['details'][url]
            listing_calls.append(url)
            return fixture['listing_body'] if len(listing_calls) == 1 else fixture['empty_body']
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_get = transport
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertLessEqual(len(listing_calls), 2)
        return captured, calls


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = 'nps-or-kr-inforls'
