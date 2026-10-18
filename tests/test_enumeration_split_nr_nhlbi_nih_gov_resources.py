"""NR-20261018: nhlbi-nih-gov-resources enumeration split (crawl() unchanged), memory-only."""
import unittest

from tests.nr_a_split_helpers import NRSplitChecks, NRSplitRunChecks


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = 'nhlbi-nih-gov-resources'


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'nhlbi-nih-gov-resources'
    END_STATUS = 200


if __name__ == '__main__':
    unittest.main()
