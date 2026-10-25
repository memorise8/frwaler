"""S-open g2: didaktorika-gr-eadd split methods equal crawl() output (synthetic HTML only)."""
import contextlib
import copy
import io
import unittest
from unittest.mock import patch

from crawler.enumeration.guard import guarded
from tests.collector_split_helpers import load

SITE = 'didaktorika-gr-eadd'
BASE = 'https://www.didaktorika.gr'

LISTING = """<html><body><form action="simple-search"></form><table>
<tr><td><a href="/eadd/handle/10442/53287">A</a></td><td><a href="/eadd/handle/10442/53287">A</a></td></tr>
<tr><td><a href="/eadd/handle/10442/8461">B</a></td></tr>
<tr><td><a href="/eadd/handle/10442/8461/stats">stats</a></td></tr>
</table></body></html>"""
EMPTY = '<html><body><form action="simple-search"></form></body></html>'
ABSTRACT = 'Synthetic abstract sentence for the regression fixture. ' * 3


def detail(title, year, ident=None, abstract=ABSTRACT, pdf=None):
    extra = f'<meta name="DC.identifier" content="{ident}">' if ident else ''
    pdf_input = f'<input type="hidden" name="pdf" value="{pdf}">' if pdf else ''
    return (f'<html><head><meta name="DC.title" xml:lang="en" content="{title}">'
            f'<meta name="DCTERMS.abstract" xml:lang="en" content="{abstract}">'
            f'<meta name="DC.date" content="{year}">'
            f'<meta name="DC.identifier" content="10.12681/eadd/{year}">{extra}'
            f'</head><body>{pdf_input}</body></html>')


DETAILS = {
    BASE + '/eadd/handle/10442/53287': detail('Thesis A', '2023', pdf='/eadd/bitstream/10442/53287/1/53287.pdf'),
    BASE + '/eadd/handle/10442/8461': detail('Thesis B', '1972', ident='8462'),
}


class DidaktorikaOpen(unittest.TestCase):
    def native(self, details):
        crawler, module = load(SITE)
        saved = []
        lists = []

        def fake_curl(url, retries=3):
            if url in details:
                return details[url].encode()
            lists.append(url)
            return (LISTING if len(lists) == 1 else EMPTY).encode()
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda p: saved.append(copy.deepcopy(p))
        with patch.object(module, '_curl', fake_curl), patch('time.sleep', lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
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
        self.assertEqual(['53287', '8461'], [r['handle_id'] for r in rows])
        self.assertEqual(BASE + '/eadd/handle/10442/53287', rows[0]['url'])
        crawler, _ = load(SITE)
        self.assertEqual([], crawler._enumeration_items(EMPTY))

    def test_papers_equal_crawl(self):
        expected = self.native(DETAILS)
        _, actual = self.split(DETAILS)
        self.assertEqual(2, len(expected))
        self.assertEqual(expected, actual)
        self.assertEqual(['53287', '8462'], [p['external_id'] for p in actual])
        self.assertEqual(BASE + '/eadd/handle/10442/8461', actual[1]['url'])
        self.assertEqual(['Thesis A', 'Thesis B'], [p['title'] for p in actual])
        self.assertEqual(['2023-01-01', '1972-01-01'], [p['published_date'] for p in actual])

    def test_short_abstract_skip_preserved(self):
        short = {u: h.replace(ABSTRACT, 'Short.') for u, h in DETAILS.items()}
        self.assertEqual([], self.native(short))
        self.assertEqual([], self.split(short)[1])


if __name__ == '__main__':
    unittest.main()
