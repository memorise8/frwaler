"""kmi-re-kr-web: board list rows + detail page -> paper, compared with the unchanged crawl()."""
import unittest

from tests.l2b_g4_split_helpers import G4SplitChecks, G4SplitRunChecks


class RegisteredSplit(G4SplitChecks, unittest.TestCase):
    SITE = 'kmi-re-kr-web'


class RegisteredSplitRun(G4SplitRunChecks, unittest.TestCase):
    SITE = 'kmi-re-kr-web'


if __name__ == '__main__':
    unittest.main()
