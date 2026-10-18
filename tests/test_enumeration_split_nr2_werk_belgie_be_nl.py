"""NR2-20261018: werk-belgie-be-nl enumeration split (crawl() unchanged), memory-only."""
import unittest

from tests.nr2_a_split_helpers import NR2SplitChecks, NRSplitRunChecks


class RegisteredSplit(NR2SplitChecks, unittest.TestCase):
    SITE = 'werk-belgie-be-nl'


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'werk-belgie-be-nl'
    END_STATUS = 200


if __name__ == '__main__':
    unittest.main()
