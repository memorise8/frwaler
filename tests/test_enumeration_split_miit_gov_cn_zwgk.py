"""miit-gov-cn-zwgk: build/unit API (JSON-wrapped HTML list, one page of up to 100)
+ detail page -> paper, compared with the unchanged crawl()."""
import unittest

from tests.l2b_g4_split_helpers import G4SplitChecks
from tests.l2b_g6_split_helpers import EmptyBodyEndRunChecks


class RegisteredSplit(G4SplitChecks, unittest.TestCase):
    SITE = 'miit-gov-cn-zwgk'


class RegisteredSplitRun(EmptyBodyEndRunChecks, unittest.TestCase):
    SITE = 'miit-gov-cn-zwgk'


if __name__ == '__main__':
    unittest.main()
