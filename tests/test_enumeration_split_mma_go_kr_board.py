"""mma-go-kr-board: the unchanged crawl() output (``self._curl_get`` served from
memory) is compared with the enumeration adapter output (_enumeration_items over
``pageIndex=N`` board pages, _enumeration_paper over detail HTML).  crawl() saves
the detail URL with ``pageIndex`` = the list page; the adapter keeps that rule."""
import unittest

from tests.collector_split_helpers import load
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks

SITE = 'mma-go-kr-board'


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = SITE

    def test_saved_url_keeps_list_page_index(self):
        crawler, _ = load(self.SITE)
        rows = crawler._enumeration_items(
            self.fixture['listing_body'],
            'https://www.mma.go.kr/board/boardList.do?gesipan_id=15&mc=mma0000392&pageIndex=7&pageUnit=10')
        self.assertEqual(
            ['https://www.mma.go.kr/board/boardView.do?gesipan_id=15&gsgeul_no=101&pageIndex=7&mc=mma0000392',
             'https://www.mma.go.kr/board/boardView.do?gesipan_id=15&gsgeul_no=102&pageIndex=7&mc=mma0000392'],
            [r['url'] for r in rows])


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = SITE


if __name__ == '__main__':
    unittest.main()
