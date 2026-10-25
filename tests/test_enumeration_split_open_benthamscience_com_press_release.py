"""benthamscience-com-press-release: _enumeration_items / _enumeration_detail give exactly the
papers crawl() saves (crawl() unchanged; post_number = /press-release-detail/{id}).
Minimal hand-written HTML."""
import unittest
from unittest.mock import patch

from tests.collector_split_helpers import load

SITE = 'benthamscience-com-press-release'
P1 = 'https://www.benthamscience.com/press-release'
P2 = 'https://www.benthamscience.com/press-release?page=2'
A = 'https://www.benthamscience.com/press-release-detail/101'
B = 'https://www.benthamscience.com/press-release-detail/102'
LIST1 = ('<html><body><news-list>'
         '<a href="/press-release-detail/101"><news-title>Alpha release</news-title>'
         '<publication-date>2025-June-30</publication-date></a>'
         '<a href="/press-release-detail/101">dup</a>'
         '<a href="/press-release-detail/102"><news-title>Beta release</news-title>'
         '<publication-date>2024-Jan-02</publication-date></a>'
         '</news-list><a rel="next" href="?page=2">next</a></body></html>')
LIST2 = '<html><body><news-list></news-list></body></html>'
DETAIL_A = ('<html><body><article-title>Alpha release (full)</article-title>'
            '<aff>Press Release Date: 01-July-2025</aff>'
            '<div class="rt"><div class="card"><div class="card-body"><p>'
            + 'Researchers report a new finding in this journal article press text. ' * 2 +
            '</p><a href="/journal/123">Current Topics</a><a href="/files/alpha.pdf">PDF</a>'
            '</div></div></div></body></html>')
DETAIL_B = '<html><body><article-title>Beta</article-title><div class="rt"><div class="card"><div class="card-body">short</div></div></div></body></html>'
PAGES = {P1: LIST1, P2: LIST2, A: DETAIL_A, B: DETAIL_B}


class Split(unittest.TestCase):
    def setUp(self):
        self.crawler, _ = load(SITE)

    def native(self):
        saved = []
        c = self.crawler
        c._delay = 0
        c._last_save_created = True
        cls = type(c)
        with patch.object(cls, '_curl_get', lambda self, url, *a, **k: (url, PAGES.get(url, ''))), \
                patch.object(cls, '_save_paper', lambda self, p: saved.append(p), create=True):
            c.crawl()
        return saved

    def test_items(self):
        items = self.crawler._enumeration_items(LIST1, P1)
        self.assertEqual([A, B], [i['url'] for i in items])
        self.assertEqual(['101', '102'], [i['post_number'] for i in items])
        self.assertEqual(['2025-06-30', '2024-01-02'], [i['listed_date'] for i in items])
        self.assertEqual([], self.crawler._enumeration_items(LIST2, P2))

    def test_papers_match_crawl(self):
        items = self.crawler._enumeration_items(LIST1, P1)
        papers = [self.crawler._enumeration_detail(PAGES[i['url']], i, i['url']) for i in items]
        self.assertIsNone(papers[1])  # abstract < 50 chars
        self.assertEqual(self.native(), [p for p in papers if p])
        p = papers[0]
        self.assertEqual(('101', '101'), (p['external_id'], p['post_number']))
        self.assertEqual(('Alpha release (full)', '2025-07-01'), (p['title'], p['published_date']))
        self.assertEqual('https://www.benthamscience.com/files/alpha.pdf', p['pdf_url'])


if __name__ == '__main__':
    unittest.main()
