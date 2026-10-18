"""NR-20261018: consult-environment-govt-nz-waste enumeration split (crawl() unchanged), memory-only."""
import unittest

from tests.nr_a_split_helpers import NRSplitChecks, NRSplitRunChecks


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = 'consult-environment-govt-nz-waste'


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'consult-environment-govt-nz-waste'
    END_STATUS = 200


if __name__ == '__main__':
    unittest.main()
