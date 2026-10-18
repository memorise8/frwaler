"""kna-forest-go-kr-kfsweb: list rows + detail page through the unchanged crawl() vs the plan adapter.

The abstract always carries the canonical detail URL (>50 chars), so crawl() never
skips an item for a short abstract; the shared short-abstract check asserts that
both paths keep the items (``short_keeps_items``).
"""
import unittest
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = 'kna-forest-go-kr-kfsweb'


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = 'kna-forest-go-kr-kfsweb'
