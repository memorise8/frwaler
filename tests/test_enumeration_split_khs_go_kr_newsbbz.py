import unittest

from tests.collector_split2_helpers import Split2Checks, Split2RunChecks
from tests.collector_split_helpers import load


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = 'khs-go-kr-newsbbz'

    def test_detail_url_carries_listing_page_index_like_crawl(self):
        # crawl() builds the detail URL with the listing page number (pageIndex).
        crawler, _ = load(self.SITE)
        url = 'https://www.khs.go.kr/newsBbz/selectNewsBbzList.do?sectionId=all_sec_1&mn=NS_01_02&pageIndex=7'
        rows = crawler._enumeration_items(self.fixture['listing_body'], url)
        self.assertEqual(['101', '102'], [r['news_item_id'] for r in rows])
        self.assertTrue(all('&pageIndex=7&' in r['detail_url'] for r in rows))
        bare = crawler._enumeration_items(self.fixture['listing_body'],
                                          'https://www.khs.go.kr/newsBbz/selectNewsBbzList.do?sectionId=all_sec_1&mn=NS_01_02')
        self.assertTrue(all('&pageIndex=1&' in r['detail_url'] for r in bare))


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = 'khs-go-kr-newsbbz'
