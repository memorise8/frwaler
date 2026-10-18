"""saemangeum-go-kr-sda: board list rows + detail page through the unchanged crawl() vs the plan adapter.

crawl() pages /sda/brd/list.do?key=2009074409621&pageIndex=N (10 rows/page) and
converts each detail page in _crawl_item. The plan reads the same pages with
``_enumeration_items(html, url)`` and converts details with
``_enumeration_paper(html, item)``; the saved paper (external_id = nttSn) must be
identical. The page past the last one keeps ``div.page_wrap`` with an empty tbody.
"""
import unittest
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = 'saemangeum-go-kr-sda'


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = 'saemangeum-go-kr-sda'
