"""canada-ca-en: _enumeration_items / _enumeration_detail give exactly the papers crawl() saves
(crawl() unchanged; post_number = URL slug without .html; detail text only replaces a listing
teaser shorter than 100 chars). Minimal hand-written HTML."""
import unittest
from unittest.mock import patch

from tests.collector_split_helpers import load

SITE = 'canada-ca-en'
A = 'https://www.canada.ca/en/dept/news/2025/06/alpha-release.html'
B = 'https://www.canada.ca/en/dept/news/2024/01/beta-statement.html'
C = 'https://www.canada.ca/en/dept/news/2023/02/gamma.html'
LONG = 'The Minister announced funding for northern communities and infrastructure projects today. ' * 2
LIST = (
    '<html><body><main>'
    f'<article class="item"><h3><a href="{A}">Alpha release</a></h3>'
    '<p><time datetime="2025-06-30">June 30, 2025</time>|Dept Name|News releases</p>'
    f'<p>{LONG}</p></article>'
    '<article class="item"><h3><a href="/en/dept/news/2024/01/beta-statement.html">Beta statement</a></h3>'
    '<p><time datetime="2024-01-02">January 2, 2024</time>|Dept Name|Statements</p>'
    '<p>Short teaser text here ok.</p></article>'
    f'<article class="item"><h3><a href="{C}">Gamma</a></h3><p>x|y</p></article>'
    '</main></body></html>')
DETAIL_B = ('<html><body><main><p class="teaser">'
            'Detailed teaser from the detail page that is clearly longer than fifty characters.</p></main></body></html>')
DETAIL_C = '<html><body><p>tiny</p></body></html>'
DETAILS = {A: '<html></html>', B: DETAIL_B, C: DETAIL_C}


class Split(unittest.TestCase):
    def setUp(self):
        self.crawler, self.module = load(SITE)

    def native(self):
        saved = []
        c = self.crawler
        c._delay = 0
        c._last_save_created = True

        def fetch(url, *a, **k):
            if 'news-results.html' in url:
                return LIST if url.endswith('&idx=0') else '<html><body><main></main></body></html>'
            return DETAILS[url]

        with patch.object(self.module, '_fetch', side_effect=fetch), \
                patch.object(self.module.time, 'sleep', lambda s: None), \
                patch.object(type(c), '_save_paper', lambda self, p: saved.append(p), create=True):
            c.crawl()
        return saved

    def test_items(self):
        items = self.crawler._enumeration_items(LIST)
        self.assertEqual([A, B, C], [i['url'] for i in items])
        self.assertEqual(['alpha-release', 'beta-statement', 'gamma'], [i['post_number'] for i in items])
        self.assertEqual('2025-06-30', items[0]['published_date'])
        self.assertEqual([], self.crawler._enumeration_items('<html><body><main></main></body></html>'))

    def test_papers_match_crawl(self):
        items = self.crawler._enumeration_items(LIST)
        papers = [self.crawler._enumeration_detail(DETAILS[i['url']], i) for i in items]
        self.assertIsNone(papers[2])
        self.assertEqual(self.native(), [p for p in papers if p])
        self.assertTrue(papers[0]['abstract'].startswith('The Minister'))
        self.assertTrue(papers[1]['abstract'].startswith('Detailed teaser'))
        self.assertEqual(('beta-statement', 'Beta statement'), (papers[1]['external_id'], papers[1]['title']))


if __name__ == '__main__':
    unittest.main()
