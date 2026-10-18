"""NR-20261018: bmbfsfj-bund-de-bmbfsfj enumeration split (crawl() unchanged), memory-only."""
import unittest
from unittest.mock import patch

from crawler.base_crawler import BaseCrawler
from tests.nr_a_split_helpers import NRSplitChecks, NRSplitRunChecks


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = 'bmbfsfj-bund-de-bmbfsfj'
    TRANSPORTS = NRSplitChecks.TRANSPORTS + ('_curl_get_text',)

    def setUp(self):
        super().setUp()
        # The harness skips __init__; give crawl() the detail_delay attribute __init__ sets.
        p = patch.object(BaseCrawler, 'detail_delay', 0, create=True); p.start(); self.addCleanup(p.stop)


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'bmbfsfj-bund-de-bmbfsfj'
    END_STATUS = 200


if __name__ == '__main__':
    unittest.main()
