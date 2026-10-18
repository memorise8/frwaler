"""gri-re-kr-web: report list rows + detail page through the unchanged crawl()
(``_build_paper``) vs the plan adapter."""
import unittest
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = 'gri-re-kr-web'


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = 'gri-re-kr-web'
