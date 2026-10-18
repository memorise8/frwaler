"""NR-20261018: justitsministeriet-dk-pressemeddelelse enumeration split (crawl() unchanged), memory-only."""
import unittest

from tests.nr_a_split_helpers import NRSplitChecks, NRSplitRunChecks


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = 'justitsministeriet-dk-pressemeddelelse'


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'justitsministeriet-dk-pressemeddelelse'
    END_STATUS = 200


if __name__ == '__main__':
    unittest.main()
