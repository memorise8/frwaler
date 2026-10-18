"""NR2-20261018: gnewsonline-it-category enumeration split (crawl() unchanged), memory-only.
WordPress category pages /page/N/ (X1 path paging); the split repeats the parsing of
_fetch_list_page()/_fetch_detail() on the page the framework fetched."""
import unittest

from tests.nr2_a_split_helpers import NR2SplitChecks, NRSplitRunChecks


class RegisteredSplit(NR2SplitChecks, unittest.TestCase):
    SITE = 'gnewsonline-it-category'


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'gnewsonline-it-category'
    END_STATUS = 404


if __name__ == '__main__':
    unittest.main()
