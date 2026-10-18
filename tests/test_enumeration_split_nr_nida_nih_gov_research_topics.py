"""NR-20261018: nida-nih-gov-research-topics enumeration split (crawl() unchanged), memory-only."""
import unittest

from tests.nr_a_split_helpers import NRSplitChecks, NRSplitRunChecks


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = 'nida-nih-gov-research-topics'


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'nida-nih-gov-research-topics'
    END_STATUS = 200


if __name__ == '__main__':
    unittest.main()
