"""S-open g2: NOAA browse/recent split methods equal crawl() output (synthetic HTML only)."""
import contextlib
import copy
import io
import unittest
from unittest.mock import patch

from crawler.enumeration.guard import guarded
from crawler.enumeration.http import PermanentFailure
from tests.collector_split_helpers import load

SITE = 'repository-library-noaa-gov-view'
DETAIL = 'https://repository.library.noaa.gov/view/noaa/'
PAD = '<!-- ' + 'x' * 600 + ' -->'
LISTING = ('<html><head><title>Recent Additions</title></head><body><form><input name="rows"></form>'
           '<a href="/view/noaa/76838">A</a><a href="/view/noaa/76838">A</a><a href="/view/noaa/76837">B</a>'
           + PAD + '</body></html>')
EMPTY = '<html><head><title>Recent Additions</title></head><body><form><input name="rows"></form>' + PAD + '</body></html>'
NOT_FOUND = '<html><head><title>Page not found</title></head><body>' + PAD + '</body></html>'
ABSTRACT = 'Synthetic abstract sentence for the regression fixture. ' * 2


def detail(title, year, abstract=ABSTRACT):
    return (f'<html><head><title>{title}</title><meta name="citation_title" content="{title}">'
            f'<meta name="citation_author" content="Doe, J."><meta name="citation_abstract" content="{abstract}">'
            f'<meta name="citation_publication_date" content="{year}">'
            f'<meta name="citation_publisher" content="NOAA | https://ror.org/x">'
            f'<meta name="citation_pdf_url" content="{DETAIL}1/noaa_1_DS1.pdf"></head><body>{PAD}</body></html>')


DETAILS = {DETAIL + '76838': detail('Report A', '2026'), DETAIL + '76837': detail('Report B', '2025')}


class NoaaOpen(unittest.TestCase):
    def native(self, details, limit=10):
        crawler, _ = load(SITE)
        saved, lists = [], []

        def fake(url, retries=3):
            if url in details:
                return details[url]
            if '/browse/recent' in url:
                lists.append(url)
                return LISTING if len(lists) == 1 else EMPTY
            return NOT_FOUND
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._check_cancelled = lambda: None
        crawler._report_source_failure = lambda *a, **k: None
        crawler._curl_get = fake
        crawler._save_paper = lambda p: saved.append(copy.deepcopy(p))
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=limit)
        return saved

    def split(self, details):
        crawler, _ = load(SITE)
        with guarded():
            rows = crawler._enumeration_items(LISTING)
            papers = [crawler._enumeration_paper(details[r['url']], r) for r in rows]
        return rows, [p for p in papers if p is not None]

    def test_rows_and_empty_and_error(self):
        rows, _ = self.split(DETAILS)
        self.assertEqual([DETAIL + '76838', DETAIL + '76837'], [r['url'] for r in rows])
        crawler, _ = load(SITE)
        self.assertEqual([], crawler._enumeration_items(EMPTY))
        with self.assertRaises(PermanentFailure):
            crawler._enumeration_items(NOT_FOUND)

    def test_papers_equal_crawl(self):
        expected = self.native(DETAILS, limit=2)
        _, actual = self.split(DETAILS)
        self.assertEqual(2, len(expected))
        self.assertEqual(expected, actual)
        self.assertEqual(['76838', '76837'], [p['post_number'] for p in actual])
        self.assertEqual(['Report A', 'Report B'], [p['title'] for p in actual])
        self.assertEqual(['2026', '2025'], [p['published_date'] for p in actual])

    def test_short_abstract_and_not_found_skip(self):
        short = {u: h.replace(ABSTRACT, 'Short.') for u, h in DETAILS.items()}
        self.assertEqual([], self.native(short))
        self.assertEqual([], self.split(short)[1])
        crawler, _ = load(SITE)
        self.assertIsNone(crawler._enumeration_paper(NOT_FOUND, {'noaa_id': '1'}))


if __name__ == '__main__':
    unittest.main()
