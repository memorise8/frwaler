"""NR-20261018: consult-environment-govt-nz-resource-management enumeration split (crawl() unchanged), memory-only."""
import unittest

from tests.nr_a_split_helpers import NRSplitChecks, NRSplitRunChecks


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = 'consult-environment-govt-nz-resource-management'
    TRANSPORTS = NRSplitChecks.TRANSPORTS + ('_fetch_url',)


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'consult-environment-govt-nz-resource-management'
    END_STATUS = 200


if __name__ == '__main__':
    unittest.main()
