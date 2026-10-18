"""NR-20261018: lne-fr-fr enumeration split (crawl() unchanged), memory-only."""
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from tests.collector_split_helpers import load
from tests.nr_a_split_helpers import NRSplitChecks, NRSplitRunChecks


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = 'lne-fr-fr'

    def native(self, fixture=None):
        # Same as NRSplitChecks.native, but _detail_delay is a method on this crawler
        # (the shared helper's `_detail_delay = 0` would break crawl()); sleep is patched.
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        captured = []; calls = []; list_calls = []

        def transport(url, *args, **kwargs):
            calls.append(url)
            if url in fixture['details']:
                return fixture['details'][url]
            list_calls.append(url)
            return fixture['listing_body'] if len(list_calls) == 1 else fixture['empty_body']

        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_get = transport
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertLessEqual(len(list_calls), 2)
        return captured, calls


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'lne-fr-fr'
    END_STATUS = 200


if __name__ == '__main__':
    unittest.main()
