"""NR2-20261018: mofa-go-kr-www enumeration split (crawl() unchanged), memory-only."""
import unittest

from tests.nr_a_split_helpers import NRSplitChecks, NRSplitRunChecks


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = 'mofa-go-kr-www'


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'mofa-go-kr-www'
    END_STATUS = 200


if __name__ == '__main__':
    unittest.main()
