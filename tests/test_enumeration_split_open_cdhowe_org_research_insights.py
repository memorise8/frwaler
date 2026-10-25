"""cdhowe-org-research-insights: _enumeration_items / _enumeration_detail give exactly the papers
crawl() saves (crawl() unchanged; post_number = body class postid-N, external_id falls back to
the slug). Minimal hand-written HTML."""
import unittest
from unittest.mock import patch

from tests.collector_split_helpers import load

SITE = 'cdhowe-org-research-insights'
P1 = 'https://cdhowe.org/research-insights/'
P2 = 'https://cdhowe.org/research-insights/page/2/'
A = 'https://cdhowe.org/publication/alpha-brief/'
B = 'https://cdhowe.org/publication/beta-note/'
LIST1 = (f'<html><body><a href="{A}">Alpha</a><a href="{A}">Alpha again</a>'
         f'<a href="https://cdhowe.org/publication/beta-note">Beta</a></body></html>')
LIST2 = f'<html><body><a href="{A}">Alpha repeat</a></body></html>'
LD = ('<script type="application/ld+json">{"@type": "Article", "headline": "Alpha Brief", '
      '"datePublished": "2025-06-30T08:00:00+00:00", "author": [{"name": "A. Writer"}]}</script>')
DETAIL_A = (f'<html><head>{LD}</head><body class="single postid-12345">'
            '<div class="elementor-shortcode"><p>' + 'Canadian fiscal policy analysis text. ' * 4 + '</p></div>'
            '<a href="https://cdhowe.org/wp-content/uploads/2025/06/alpha.pdf">PDF</a></body></html>')
DETAIL_B = '<html><head><meta property="og:title" content="Beta"></head><body><p>tiny</p></body></html>'
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
        with patch.object(cls, '_curl', lambda self, url, **k: PAGES.get(url)), \
                patch.object(cls, '_save_paper', lambda self, p: saved.append(p), create=True):
            c.crawl()
        return saved

    def test_items(self):
        self.assertEqual([{'url': A}, {'url': B}], self.crawler._enumeration_items(LIST1))
        self.assertEqual([], self.crawler._enumeration_items('<html><body>none</body></html>'))

    def test_papers_match_crawl(self):
        items = self.crawler._enumeration_items(LIST1)
        papers = [self.crawler._enumeration_detail(PAGES[i['url']], i) for i in items]
        self.assertIsNone(papers[1])  # abstract < 50 chars
        self.assertEqual(self.native(), [p for p in papers if p])
        p = papers[0]
        self.assertEqual(('12345', '12345'), (p['external_id'], p['post_number']))
        self.assertEqual(('Alpha Brief', '2025-06-30'), (p['title'], p['published_date']))
        self.assertEqual('https://cdhowe.org/wp-content/uploads/2025/06/alpha.pdf', p['pdf_url'])


if __name__ == '__main__':
    unittest.main()
