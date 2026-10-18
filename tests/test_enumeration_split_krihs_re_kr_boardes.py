"""krihs-re-kr-boardes: the unchanged crawl() output (module ``_curl_get`` served
from memory) is compared with the enumeration adapter output (_enumeration_items
over ``nPage=N`` board pages, _enumeration_paper over detail HTML)."""
import unittest

from tests.collector_split2_helpers import Split2Checks, Split2RunChecks

SITE = 'krihs-re-kr-boardes'


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = SITE


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = SITE


if __name__ == '__main__':
    unittest.main()
