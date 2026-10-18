"""NR-20261018: bmwsb-bund-de-de enumeration split (crawl() unchanged), memory-only."""
import unittest

from tests.nr_a_split_helpers import NRSplitChecks, NRSplitRunChecks


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = 'bmwsb-bund-de-de'


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'bmwsb-bund-de-de'
    END_STATUS = 200


if __name__ == '__main__':
    unittest.main()
