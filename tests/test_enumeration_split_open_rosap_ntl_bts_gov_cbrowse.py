"""S-open g2: ROSAP cbrowse split methods equal crawl() output (synthetic HTML only)."""
import contextlib
import copy
import io
import unittest
from unittest.mock import patch

from crawler.enumeration.guard import guarded
from tests.collector_split_helpers import load

SITE = 'rosap-ntl-bts-gov-cbrowse'
BASE = 'https://rosap.ntl.bts.gov'
LISTING = ('<html><body><form><input name="terms"><input name="pid"></form>'
           '<a href="/view/dot/93529">A</a><a href="/view/dot/93529">A</a>'
           '<a href="/view/dot/92934">B</a><a href="/view/dot/92934/dot_92934_DS1.pdf">pdf</a></body></html>')
EMPTY = '<html><body><form><input name="terms"><input name="pid"></form></body></html>'
ABSTRACT = 'Synthetic abstract sentence for the regression fixture. ' * 2


def detail(title, date, abstract=ABSTRACT):
    return (f'<html><head><meta name="citation_title" content="{title}">'
            f'<meta name="citation_abstract" content="{abstract}">'
            f'<meta name="citation_publication_date" content="{date}">'
            f'<meta name="citation_author" content="Doe, J.">'
            f'<meta name="citation_pdf_url" content="/view/dot/1/dot_1_DS1.pdf"></head>'
            f'<body><p><span>Corporate Publisher:</span> <a href="/x">Volpe</a></p></body></html>')


DETAILS = {BASE + '/view/dot/93529': detail('Report A', '2026/09/18'),
           BASE + '/view/dot/92934': detail('Report B', '2026/08/10')}


class RosapOpen(unittest.TestCase):
    def native(self, details):
        crawler, _ = load(SITE)
        saved, lists = [], []

        def fake(url, *, timeout=60):
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
        self.assertEqual(['93529', '92934'], [r['item_id'] for r in rows])
        crawler, _ = load(SITE)
        self.assertEqual([], crawler._enumeration_items(EMPTY))

    def test_papers_equal_crawl(self):
        expected = self.native(DETAILS)
        _, actual = self.split(DETAILS)
        self.assertEqual(2, len(expected))
        self.assertEqual(expected, actual)
        self.assertEqual([BASE + '/view/dot/93529', BASE + '/view/dot/92934'], [p['url'] for p in actual])
        self.assertEqual(['93529', '92934'], [p['external_id'] for p in actual])
        self.assertEqual(['2026-09-18', '2026-08-10'], [p['published_date'] for p in actual])
        self.assertEqual('Volpe', actual[0]['publisher'])

    def test_short_abstract_skip_preserved(self):
        short = {u: h.replace(ABSTRACT, 'Short.') for u, h in DETAILS.items()}
        self.assertEqual([], self.native(short))
        self.assertEqual([], self.split(short)[1])


if __name__ == '__main__':
    unittest.main()
