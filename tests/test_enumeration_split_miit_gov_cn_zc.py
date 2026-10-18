"""miit-gov-cn-zc: search-API list rows + detail page -> paper, compared with the unchanged crawl()."""
import unittest

from tests.l2b_g6_split_helpers import EmptyBodyEndRunChecks, ModuleCurlSplitChecks


class RegisteredSplit(ModuleCurlSplitChecks, unittest.TestCase):
    SITE = 'miit-gov-cn-zc'


class RegisteredSplitRun(EmptyBodyEndRunChecks, unittest.TestCase):
    SITE = 'miit-gov-cn-zc'


if __name__ == '__main__':
    unittest.main()
