"""S-open g2: CNIC news split methods equal crawl() output (synthetic HTML only)."""
import contextlib
import copy
import io
import unittest
from unittest.mock import patch

from crawler.enumeration.guard import guarded
from tests.collector_split_helpers import load

SITE = 'cnic-es-en'
BASE = 'https://www.cnic.es'


def row(n, href, title, date, intro):
    return (f'<tr class="row-{n}"><td><div class="views-field-title"><a href="{href}">{title}</a></div>'
            f'<span property="dc:date" content="{date}T10:00:00+02:00">x</span>'
            f'<div class="views-field-field-notice-type"><div class="field-content">News</div></div>'
            f'<div class="views-field-field-introduction"><div class="field-content">{intro}</div></div></td></tr>')


LISTING = ('<html><body><div class="view view-Buscadores-noticias"><table>'
           + row(1, '/en/noticias/story-a', 'Story A', '2026-07-15', 'Intro A')
           + row(2, '/en/noticias/story-b', 'Story B', '2026-07-01', 'Intro B')
           + '</table></div><ul><li class="pager-next"><a href="/en/actualidad/noticias?page=1">next</a></li></ul>'
           + '</body></html>')
EMPTY = '<html><body><div class="view view-Buscadores-noticias"></div></body></html>'
BODY = 'Synthetic body text for the regression fixture. ' * 3
DETAILS = {
    BASE + '/en/noticias/story-a': (f'<html><body class="page-node page-node-241702">'
                                    f'<div class="field-name-field-description"><div class="field-item">{BODY}</div></div>'
                                    f'<span property="dc:date" content="2026-07-15T10:00:00+02:00">x</span></body></html>'),
    # no node id and no body: slug identity, abstract built from intro + context
    BASE + '/en/noticias/story-b': '<html><body class="page"></body></html>',
}


class CnicOpen(unittest.TestCase):
    def native(self, details):
        crawler, _ = load(SITE)
        saved, lists = [], []

        def fake(url):
            if url in details:
                return details[url]
            lists.append(url)
            return LISTING if len(lists) == 1 else EMPTY
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._curl = fake
        crawler._save_paper = lambda p: saved.append(copy.deepcopy(p))
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=10)
        return saved

    def split(self, details):
        crawler, _ = load(SITE)
        with guarded():
            rows = crawler._enumeration_items(LISTING)
            papers = [crawler._enumeration_paper(details[r['url']], r) for r in rows]
        return rows, [p for p in papers if p is not None]

    def test_rows(self):
        rows, _ = self.split(DETAILS)
        self.assertEqual([BASE + '/en/noticias/story-a', BASE + '/en/noticias/story-b'], [r['url'] for r in rows])
        self.assertEqual('2026-07-15', rows[0]['published_date'])
        crawler, _ = load(SITE)
        self.assertEqual([], crawler._enumeration_items(EMPTY))

    def test_papers_equal_crawl(self):
        expected = self.native(DETAILS)
        _, actual = self.split(DETAILS)
        self.assertEqual(2, len(expected))
        self.assertEqual(expected, actual)
        self.assertEqual(['241702', 'story-b'], [p['external_id'] for p in actual])
        self.assertEqual(['241702', None], [p['post_number'] for p in actual])
        self.assertEqual(['Story A', 'Story B'], [p['title'] for p in actual])
        self.assertEqual(['2026-07-15', '2026-07-01'], [p['published_date'] for p in actual])


if __name__ == '__main__':
    unittest.main()
