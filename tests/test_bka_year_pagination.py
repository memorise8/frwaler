import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'bka_year_pagination', ROOT / 'crawler/sites/custom/bundeskanzleramt-gv-at-bundeskanzleramt.py'
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
FIXTURE = ROOT / 'tests/fixtures/bka_year_pagination'
SOURCES = json.loads((FIXTURE / 'sources.json').read_text())['records']
PAGES = {r['source_url']: (FIXTURE / r['fixture']).read_text() for r in SOURCES}


class BkaYearPaginationTests(unittest.TestCase):
    def test_all_published_2026_pages_and_memberships_are_visited(self):
        with patch.object(MODULE, '_curl_get', side_effect=PAGES.__getitem__) as fetch:
            pages = list(MODULE._iter_year_pages(['2026'], None, lambda: False))
        self.assertEqual([call.args[0] for call in fetch.call_args_list], list(PAGES))
        expected = [a['href'] for raw in PAGES.values()
                    for a in MODULE._make_soup(raw).select('main a.card-link')]
        actual = [a['href'] for year, soup in pages for a in soup.select('main a.card-link')]
        self.assertGreater(len(actual), 49)
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), len(set(actual)))

    def test_cancel_after_first_page_does_not_request_the_next_page(self):
        stopped = False
        with patch.object(MODULE, '_curl_get', side_effect=PAGES.__getitem__) as fetch:
            pages = MODULE._iter_year_pages(['2026'], None, lambda: stopped)
            next(pages)
            stopped = True
            self.assertEqual(list(pages), [])
        self.assertEqual(fetch.call_count, 1)

    def test_failure_on_second_page_is_not_reported_as_exhaustion(self):
        with patch.object(MODULE, '_curl_get', side_effect=[next(iter(PAGES.values())), None]):
            with self.assertRaisesRegex(RuntimeError, 'currentPage=1'):
                list(MODULE._iter_year_pages(['2026'], None, lambda: False))

    def test_repeated_or_cross_year_next_link_is_an_explicit_error(self):
        first_url, raw = next(iter(PAGES.items()))
        for destination in ('?currentPage=0', first_url.replace('2026', '2025') + '?currentPage=1'):
            with self.subTest(destination=destination):
                soup = MODULE._make_soup(raw)
                soup.select_one('li.page-next a')['href'] = destination
                with self.assertRaises(ValueError):
                    MODULE._next_year_page(soup, first_url)


if __name__ == '__main__':
    unittest.main()
