"""NR-20261018: niehs-nih-gov-newsreleases enumeration split (crawl() unchanged), memory-only."""
import unittest

from tests.nr_a_split_helpers import NRSplitChecks, NRSplitRunChecks


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = 'niehs-nih-gov-newsreleases'


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'niehs-nih-gov-newsreleases'
    END_STATUS = 200


if __name__ == '__main__':
    unittest.main()
