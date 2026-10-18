import contextlib
import copy
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.collector_split2_helpers import Split2Checks, Split2RunChecks
from tests.collector_split_helpers import load


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = 'nin-nl-publications'

    def native(self, fixture=None):
        """crawl() fetches through BaseCrawler._request(url, headers=...)."""
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        captured, calls, listing_calls = [], [], []

        def request(url, headers=None, **kwargs):
            calls.append(url)
            if url in fixture['details']:
                return SimpleNamespace(content=fixture['details'][url].encode('utf-8'))
            listing_calls.append(url)
            body = fixture['listing_body'] if len(listing_calls) == 1 else fixture['empty_body']
            return SimpleNamespace(content=body.encode('utf-8'))

        crawler._delay = 0
        crawler._last_save_created = True
        crawler._request = request
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertLessEqual(len(listing_calls), 2)
        return captured, calls


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = 'nin-nl-publications'
