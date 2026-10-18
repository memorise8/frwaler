import contextlib
import copy
import io
import unittest
from unittest.mock import patch

from tests.collector_split_helpers import load
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = 'polytechnique-edu-presse'

    def native(self, fixture=None):
        """Unchanged crawl() with an in-memory _fetch(url) transport."""
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        captured = []; calls = []; listing_calls = []
        def fetch(url):
            calls.append(url)
            if url in fixture['details']:
                return fixture['details'][url]
            listing_calls.append(url)
            return fixture['listing_body'] if len(listing_calls) == 1 else fixture['empty_body']
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._fetch = fetch
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertLessEqual(len(listing_calls), 2)
        return captured, calls


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = 'polytechnique-edu-presse'
