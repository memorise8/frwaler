"""cac-gov-cn-hdfw: _enumeration_items / _enumeration_detail give exactly the papers crawl()
saves (crawl() unchanged; post_number = c_<id> of the detail URL). Minimal hand-written data."""
import json
import unittest
from unittest.mock import patch

from tests.collector_split_helpers import load

SITE = 'cac-gov-cn-hdfw'
A = 'https://www.cac.gov.cn/2025-12/30/c_1767000000000001.htm'
B = 'https://www.cac.gov.cn/2024-01/02/c_1704000000000002.htm'
C = 'https://www.cac.gov.cn/2023-02/01/c_1675000000000003.htm'
LONG_DESC = '描述' * 30
PAGE1 = {'totalRec': 3, 'list': [
    {'infourl': '//www.cac.gov.cn/2025-12/30/c_1767000000000001.htm', 'topic': ' 公告甲 ',
     'description': '', 'pubtime': '2025-12-30 17:15:00.0', 'filltime': '2025-12-29 10:00:00.0',
     'coverurl': '//www.cac.gov.cn/cover.jpg'},
    {'infourl': '', 'topic': 'no url'},
    {'infourl': '/2024-01/02/c_1704000000000002.htm', 'topic': '公告乙', 'description': LONG_DESC,
     'pubtime': '2024-01-02 09:00:00.0', 'filltime': ''},
    {'infourl': C, 'topic': '公告丙', 'description': 'short', 'pubtime': '2023-02-01 09:00:00.0'},
]}
DETAILS = {
    A: '<html><body><div id="BodyLabel"><p>' + '正文内容' * 20 + '</p></div></body></html>',
    B: '<html><body><div id="BodyLabel">短</div></body></html>',
    C: '<html><body><p>x</p></body></html>',
}


class Split(unittest.TestCase):
    def setUp(self):
        self.crawler, _ = load(SITE)

    def native(self):
        saved = []
        c = self.crawler
        c._delay = 0
        c._last_save_created = True
        cls = type(c)
        pages = {1: json.dumps(PAGE1), 2: json.dumps({'list': []})}
        with patch.object(cls, '_curl_post_list', lambda self, n: pages[n]), \
                patch.object(cls, '_curl_get', lambda self, url: DETAILS[url]), \
                patch.object(cls, '_save_paper', lambda self, p: saved.append(p), create=True):
            c.crawl()
        return saved

    def test_items(self):
        items = self.crawler._enumeration_items(PAGE1)
        self.assertEqual([A, B, C], [i['url'] for i in items])
        self.assertEqual('2025-12-30', items[0]['published_date'])
        self.assertEqual([], self.crawler._enumeration_items({'list': []}))

    def test_papers_match_crawl(self):
        items = self.crawler._enumeration_items(PAGE1)
        papers = [self.crawler._enumeration_detail(DETAILS[i['url']], i) for i in items]
        self.assertIsNone(papers[2])
        self.assertEqual(self.native(), [p for p in papers if p])
        self.assertEqual(('1767000000000001', '公告甲', '2025-12-30'),
                         (papers[0]['post_number'], papers[0]['title'], papers[0]['published_date']))
        self.assertEqual(LONG_DESC, papers[1]['abstract'])  # description fallback


if __name__ == '__main__':
    unittest.main()
