import unittest
from crawler.enumeration.http import PermanentFailure
from tests.collector_split_helpers import load
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks

LIST_URL = 'https://www.mpva.go.kr/mpva/selectBbsNttList.do?bbsNo=16&key=77&pageIndex=3'


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = 'mpva-go-kr-mpva'

    def test_added_listing_rejects_wrong_shape(self):
        # The list method also takes the page URL (pageIndex is part of crawl()'s detail URL).
        crawler, _ = load(self.SITE)
        for bad in ({'error': 'not a listing'}, '<html><body>maintenance</body></html>'):
            with self.subTest(bad=bad):
                with self.assertRaises(PermanentFailure) as ctx:
                    crawler._enumeration_items(bad, LIST_URL)
                self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_detail_url_carries_the_listing_page_like_crawl(self):
        crawler, _ = load(self.SITE)
        rows = crawler._enumeration_items(self.fixture['listing_body'], LIST_URL)
        self.assertEqual(2, len(rows))
        self.assertTrue(all(r['url'].endswith('&pageIndex=3') for r in rows))
        # A page-less URL is crawl()'s first page.
        bare = crawler._enumeration_items(self.fixture['listing_body'], LIST_URL.split('&pageIndex')[0])
        self.assertEqual(list(self.fixture['details']), [r['url'] for r in bare])


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = 'mpva-go-kr-mpva'
