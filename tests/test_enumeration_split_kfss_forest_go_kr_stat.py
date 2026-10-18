"""kfss-forest-go-kr-stat: JSON article rows + per-article file-list JSON (as the detail)
through the unchanged crawl() vs the plan adapter.

The abstract always carries registration date and source name, so shortening the
article body does not make crawl() skip an item (``short_keeps_items``).
"""
import unittest
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = 'kfss-forest-go-kr-stat'


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = 'kfss-forest-go-kr-stat'
