import contextlib
import copy
import io
import unittest
from unittest.mock import patch

from tests.collector_split_helpers import load
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = 'nps-or-kr-pnsgdnc'

    def native(self, fixture=None):
        """Unchanged crawl() with in-memory _curl_post (listing) / _curl_get (detail)."""
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        captured = []; calls = []; listing_calls = []
        def post(url, data, retries=3):
            listing_calls.append((url, dict(data)))
            return fixture['listing_body'] if len(listing_calls) == 1 else fixture['empty_body']
        def get(url, retries=3):
            calls.append(url)
            return fixture['details'].get(url)
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_post = post
        crawler._curl_get = get
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertLessEqual(len(listing_calls), 2)
        # The plan's GET listing carries the same fields crawl() POSTs.
        posted = listing_calls[0][1]
        listing = self.plan['sites'][self.SITE]['listing']
        self.assertEqual(listing['url'], listing_calls[0][0])
        self.assertEqual({k: str(v) for k, v in listing['params'].items()}, posted)
        return captured, calls


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = 'nps-or-kr-pnsgdnc'
