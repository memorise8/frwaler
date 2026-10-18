"""NR-20261018: nidcd-nih-gov-order enumeration split (crawl() unchanged), memory-only."""
import unittest

from tests.nr_a_split_helpers import NRSplitChecks, NRSplitRunChecks


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = 'nidcd-nih-gov-order'


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'nidcd-nih-gov-order'
    END_STATUS = 200


if __name__ == '__main__':
    unittest.main()
