"""NR-20261018: niaaa-nih-gov-news-events enumeration split (crawl() unchanged), memory-only."""
import unittest

from tests.nr_a_split_helpers import NRSplitChecks, NRSplitRunChecks


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = 'niaaa-nih-gov-news-events'


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'niaaa-nih-gov-news-events'
    END_STATUS = 200


if __name__ == '__main__':
    unittest.main()
