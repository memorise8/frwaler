"""forest-go-kr-kfsweb: board list rows + detail page through the unchanged crawl() vs the plan adapter."""
import unittest
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = 'forest-go-kr-kfsweb'


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = 'forest-go-kr-kfsweb'
