"""harcresearch-org-news: _enumeration_items / _enumeration_detail give exactly the paper
crawl() saves (crawl() unchanged; post_number = URL slug). Minimal hand-written HTML."""
import unittest
from unittest.mock import patch

from tests.collector_split_helpers import load

SITE = 'harcresearch-org-news'
LIST_URL = 'https://harcresearch.org/news/'
URL_A = 'https://harcresearch.org/news/alpha-story/'
URL_B = 'https://harcresearch.org/news/beta-story/'
LISTING = (
    '<html><body><div class="grid">'
    f'<div class="item news"><a href="{URL_A}"><span class="h3">Alpha <b>story</b></span>'
    '<span class="h6">News</span><span class="h5">03.14.2025</span></a></div>'
    f'<div class="item event"><a href="{URL_B}"><span class="h3">Beta story</span>'
    '<span class="h6">Events</span><span class="h5">01.02.2024</span></a></div>'
    '</div></body></html>')
BODY = 'This paragraph is long enough to count as article body text for the crawler test. ' * 2
DETAIL_A = (f'<html><body><nav>menu</nav><div class="content-block"><p>{BODY}</p>'
            '<p><a href="https://www.energy.gov/files/report-a.pdf">PDF</a></p></div></body></html>')
DETAIL_B = '<html><body><div class="content-block"><p>short</p></div></body></html>'
PAGES = {LIST_URL: LISTING, URL_A: DETAIL_A, URL_B: DETAIL_B}


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

    def split(self):
        items = self.crawler._enumeration_items(LISTING)
        papers = [self.crawler._enumeration_detail(PAGES[i['url']], i) for i in items]
        return items, papers

    def test_items(self):
        items, _ = self.split()
        self.assertEqual([URL_A, URL_B], [i['url'] for i in items])
        self.assertEqual(['Alpha story', 'Beta story'], [i['title'] for i in items])
        self.assertEqual(['03.14.2025', '01.02.2024'], [i['date_raw'] for i in items])

    def test_paper_matches_crawl(self):
        _, papers = self.split()
        self.assertIsNone(papers[1])  # abstract < 100 chars: crawl() skips it too
        self.assertEqual(self.native(), [p for p in papers if p])
        p = papers[0]
        self.assertEqual(('alpha-story', 'alpha-story'), (p['external_id'], p['post_number']))
        self.assertEqual('2025-03-14', p['published_date'])
        self.assertEqual('https://www.energy.gov/files/report-a.pdf', p['pdf_url'])

    def test_empty_listing(self):
        self.assertEqual([], self.crawler._enumeration_items(''))


if __name__ == '__main__':
    unittest.main()
