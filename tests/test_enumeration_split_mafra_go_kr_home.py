"""mafra-go-kr-home: crawl() reads page 1 from subview.do and later pages by POST
(``self._curl``); the enumeration plan reads the same board by GET
``artclList.do?page=N&row=10``. The unchanged crawl() output (memory transport) is
compared with the adapter output."""
import contextlib
import copy
import io
import unittest
from unittest.mock import patch

from tests.collector_split_helpers import load
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks

SITE = 'mafra-go-kr-home'


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, module = load(self.SITE)
        captured = []; calls = []; listing_calls = []

        def curl(url, data=None, referer=None, headers_only=False):
            self.assertFalse(headers_only, 'fixture attachments carry file names')
            calls.append(url)
            if url in fixture['details']:
                return fixture['details'][url]
            listing_calls.append(url)
            return fixture['listing_body'] if len(listing_calls) == 1 else fixture['empty_body']

        crawler.detail_delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl = curl
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch('time.sleep', lambda *a: None))
            with contextlib.redirect_stdout(io.StringIO()):
                result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertLessEqual(len(listing_calls), 2)
        return captured, calls

    def test_list_page_number_comes_from_url(self):
        crawler, _ = load(self.SITE)
        rows = crawler._enumeration_items(self.fixture['listing_body'],
                                          'https://www.mafra.go.kr/bbs/home/792/artclList.do?page=7&row=10')
        self.assertEqual([7, 7], [r['page'] for r in rows])


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = SITE


if __name__ == '__main__':
    unittest.main()
