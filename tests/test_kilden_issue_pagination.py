"""Retained journal indexes must all reach native issue discovery."""
import contextlib
import importlib.util
import io
import unittest
from pathlib import Path
from unittest.mock import patch
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / 'fixtures/kilden_issue_index'


def load():
    spec = importlib.util.spec_from_file_location(
        'kilden_index', ROOT / 'crawler/sites/custom/kjonnsforskning-no-nb.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cls = next(value for value in vars(module).values()
               if isinstance(value, type) and getattr(value, 'site_id', None) == 'kjonnsforskning-no-nb')
    obj = object.__new__(cls)
    obj._delay = 0
    obj._check_cancelled = lambda: None
    failures = []
    obj._report_source_failure = failures.append
    return module, obj, failures


def replay(module, obj, bodies):
    requested = []

    def fetch(url):
        requested.append(url)
        return bodies.get(url)  # Issue bodies are deliberately not fetched or fabricated.

    with patch.object(module, '_curl_get', fetch), patch.object(module.time, 'sleep', lambda _: None), \
            contextlib.redirect_stdout(io.StringIO()):
        saved = obj.crawl()
    return saved, requested


class KildenIssuePagination(unittest.TestCase):
    def test_native_crawl_discovers_all190_source_issues_preserving_first24(self):
        module, obj, failures = load()
        bodies = {module._START_URL if page == 0 else module._START_URL + '?page=' + str(page):
                  (FIXTURES / f'page-{page}.html').read_text() for page in range(8)}
        expected = [module._BASE_URL + link['href'] for raw in bodies.values()
                    for link in BeautifulSoup(raw, 'lxml').select('a[href^="/nb/tidsskriftet/"]')]
        old_first = [module._BASE_URL + path for path in module._get_issue_paths(bodies[module._START_URL])]
        saved, requested = replay(module, obj, bodies)
        issues = [url for url in requested if '/nb/tidsskriftet/' in url]
        self.assertEqual(saved, 0)
        self.assertEqual(len(set(issues)), 190)
        self.assertEqual(issues, expected)
        self.assertEqual(issues[:24], old_first)
        self.assertEqual(requested[:8], list(bodies))
        self.assertEqual(failures, [])

    def test_missing_next_index_reports_partial_discovery(self):
        module, obj, failures = load()
        saved, requested = replay(module, obj, {module._START_URL: (FIXTURES / 'page-0.html').read_text()})
        self.assertEqual(saved, 0)
        self.assertEqual(len([url for url in requested if '/nb/tidsskriftet/' in url]), 24)
        self.assertEqual(failures, ['Cannot fetch journal index: ' + module._START_URL + '?page=1'])

    def test_repeated_page_content_cannot_look_like_completed_pagination(self):
        module, obj, failures = load()
        raw = (FIXTURES / 'page-0.html').read_text()
        _, requested = replay(module, obj, {module._START_URL: raw, module._START_URL + '?page=1': raw})
        self.assertEqual(len([url for url in requested if '/nb/tidsskriftet/' in url]), 24)
        self.assertEqual(failures, ['Journal index repeats issue entries: ' + module._START_URL + '?page=1'])

    def test_index_next_link_stays_on_configured_source(self):
        module, _, _ = load()
        for href in ['https://example.org/next', '/nb/some-other-index?page=1',
                     'http://kjonnsforskning.no/nb/tidsskrift-for-kjonnsforskning?page=1']:
            with self.subTest(href=href), self.assertRaises(ValueError):
                module._get_next_issue_index_url(
                    '<li class="pager__item--next"><a href="' + href + '">Next</a></li>', module._START_URL)


if __name__ == '__main__':
    unittest.main()
