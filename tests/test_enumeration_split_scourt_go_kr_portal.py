"""scourt-go-kr-portal: board list rows + detail page through the unchanged crawl() vs the plan adapter.

crawl() pages /portal/news/NewsListAction.work?gubun=6&pageIndex=N (10 rows/page)
and converts each NewsViewAction.work?seqnum=N&gubun=6 page inline. The plan reads
the same pages with ``_enumeration_items(html, url)`` and converts details with
``_enumeration_paper(html, item)``; the saved paper (external_id = seqnum) must be
identical. A page past the last one keeps ``div.pagelist`` with an empty tbody.
"""
import unittest
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = 'scourt-go-kr-portal'


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = 'scourt-go-kr-portal'
