"""NR-20261018: nigms-nih-gov-news enumeration split (crawl() unchanged), memory-only."""
import unittest

from tests.nr_a_split_helpers import NRSplitChecks, NRSplitRunChecks


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = 'nigms-nih-gov-news'


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'nigms-nih-gov-news'
    END_STATUS = 200


if __name__ == '__main__':
    unittest.main()
