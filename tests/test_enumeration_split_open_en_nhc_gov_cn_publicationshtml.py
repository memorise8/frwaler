"""en-nhc-gov-cn-publicationshtml: _enumeration_items / _enumeration_detail give exactly the
papers crawl() saves (crawl() unchanged; post_number = enpproperty articleid, else c_<id>).
Minimal hand-written HTML."""
import unittest
from unittest.mock import patch

from tests.collector_split_helpers import load

SITE = 'en-nhc-gov-cn-publicationshtml'
LIST_URL = 'https://en.nhc.gov.cn/publications.html'
A = 'https://en.nhc.gov.cn/2020-09/07/c_81565.htm'
B = 'https://en.nhc.gov.cn/2012-12/27/c_45000.htm'
LIST = ('<html><body><div class="list"><a href="2020-09/07/c_81565.htm">Alpha report</a>'
        '<a href="javascript:void(0)">x</a><a href="/2012-12/27/c_45000.htm">Beta</a>'
        '<a href="2020-09/07/c_81565.htm">Alpha report</a></div></body></html>')
REGEX_LIST = '<html><body><a href="/2012-12/27/c_45000.htm">Beta <b>b</b></a><a href="/about.html">About</a></body></html>'
DETAIL_A = ('<html><!--enpproperty <articleid>81565</articleid><date>2020-09-07 17:47:15.0</date>'
            '<title>Alpha report full</title><keyword>health</keyword><nodeid>2</nodeid>/enpproperty-->'
            '<body><!--enpcontent--><p>' + 'Statistical bulletin on health development text. ' * 3 +
            '<a href="/file/alpha.pdf">pdf</a></p><!--/enpcontent--></body></html>')
DETAIL_B = '<html><body><div class="art-text">short</div></body></html>'
PAGES = {LIST_URL: LIST, A: DETAIL_A, B: DETAIL_B}


class Split(unittest.TestCase):
    def setUp(self):
        self.crawler, self.module = load(SITE)

    def native(self):
        saved = []
        c = self.crawler
        c._delay = 0
        c._last_save_created = True
        with patch.object(self.module, '_curl_get', side_effect=lambda url, *a, **k: PAGES[url]), \
                patch.object(type(c), '_save_paper', lambda self, p: saved.append(p), create=True):
            c.crawl()
        return saved

    def test_items(self):
        self.assertEqual([{'url': A, 'title': 'Alpha report'}, {'url': B, 'title': 'Beta'}],
                         self.crawler._enumeration_items(LIST))
        self.assertEqual([{'url': B, 'title': 'Beta b'}], self.crawler._enumeration_items(REGEX_LIST))

    def test_papers_match_crawl(self):
        items = self.crawler._enumeration_items(LIST)
        papers = [self.crawler._enumeration_detail(PAGES[i['url']], i) for i in items]
        self.assertIsNone(papers[1])
        self.assertEqual(self.native(), [p for p in papers if p])
        p = papers[0]
        self.assertEqual(('81565', 'Alpha report full', '2020-09-07'),
                         (p['post_number'], p['title'], p['published_date']))
        self.assertEqual('https://en.nhc.gov.cn/file/alpha.pdf', p['pdf_url'])


if __name__ == '__main__':
    unittest.main()
