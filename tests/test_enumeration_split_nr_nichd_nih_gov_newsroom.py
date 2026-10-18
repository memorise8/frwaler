"""NR-20261018: nichd-nih-gov-newsroom enumeration split (crawl() unchanged), memory-only."""
import unittest

from tests.nr_a_split_helpers import NRSplitChecks, NRSplitRunChecks


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = 'nichd-nih-gov-newsroom'


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'nichd-nih-gov-newsroom'
    END_STATUS = 200


if __name__ == '__main__':
    unittest.main()
