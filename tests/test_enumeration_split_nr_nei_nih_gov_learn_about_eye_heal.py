"""NR-20261018: nei-nih-gov-learn-about-eye-heal enumeration split (crawl() unchanged), memory-only."""
import unittest

from tests.nr_a_split_helpers import NRSplitChecks, NRSplitRunChecks


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = 'nei-nih-gov-learn-about-eye-heal'


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'nei-nih-gov-learn-about-eye-heal'
    END_STATUS = 200


if __name__ == '__main__':
    unittest.main()
