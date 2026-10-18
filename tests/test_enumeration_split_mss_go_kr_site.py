import contextlib
import copy
import io
import unittest
from unittest.mock import patch

from tests.collector_split2_helpers import Split2Checks, Split2RunChecks
from tests.collector_split_helpers import load

DETAIL = ("https://www.mss.go.kr/site/smba/foffice/ex/statDB/"
          "StReportContentDetailView.do?gb=1&nodeId=&rcCode=&roCode=&reSeq=")


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = 'mss-go-kr-site'

    def native(self, fixture=None):
        """crawl() fetches pages through its own _fetch_list/_fetch_detail (detail by POST reSeq)."""
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        captured, calls, listing_calls = [], [], []

        def fetch_list(page):
            listing_calls.append(page)
            return fixture['listing_body'] if page == 1 else fixture['empty_body']

        def fetch_detail(re_seq):
            calls.append(DETAIL + str(re_seq))
            return fixture['details'][DETAIL + str(re_seq)]

        crawler._delay = 0
        crawler._last_save_created = True
        crawler._fetch_list = fetch_list
        crawler._fetch_detail = fetch_detail
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertLessEqual(len(listing_calls), 2)
        return captured, calls


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = 'mss-go-kr-site'
