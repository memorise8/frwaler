"""Retained Drupal source links must survive native press-list discovery."""
import contextlib
import importlib.util
import io
import unittest
from pathlib import Path
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / 'fixtures/inria_press/retained-126-cards.html'


def discover(raw):
    spec = importlib.util.spec_from_file_location('inria_press', ROOT / 'crawler/sites/custom/inria-fr-fr.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    obj = object.__new__(module.InriaFrFrCrawler)
    obj._delay = 0
    obj._check_cancelled = lambda: None
    obj._report_source_failure = lambda _: None
    attempted = []

    def fetch(url, **kwargs):
        if url == obj._START_URL:
            return raw
        attempted.append(url)
        return None  # No detail fetch or save is performed in this discovery test.

    obj._curl_get = fetch
    with contextlib.redirect_stdout(io.StringIO()):
        obj._crawl_impl()
    return attempted, obj


class InriaDiscovery(unittest.TestCase):
    def test_retained_126_source_links_include_three_encoded_front_controller_paths(self):
        raw = FIXTURE.read_text()
        expected = ['https://www.inria.fr' + x['about']
                    for x in BeautifulSoup(raw, 'lxml').select('article.news')]
        found, obj = discover(raw)
        self.assertEqual(len(found), 126)
        self.assertEqual(found, expected)
        encoded = [x for x in found if '/index%2Ephp/fr/' in x]
        self.assertEqual(len(encoded), 3)
        for url in encoded:
            parsed = obj._parse_detail(BeautifulSoup('<main></main>', 'lxml'), url)
            self.assertEqual(parsed['external_id'], url.rsplit('/', 1)[-1])

    def test_language_and_origin_restrictions_and_original_encoding_are_preserved(self):
        paths = ['/fr/report', '/index.php/fr/second', '/index%2ephp/fr/third',
                 '/index.php/en/fourth', 'https://example.org/fr/external',
                 '//example.org/fr/external', '/other/fr/invalid']
        raw = ''.join('<article class="news" about="' + p + '"><h3>Title</h3></article>' for p in paths)
        found, _ = discover(raw)
        self.assertEqual(found, ['https://www.inria.fr' + p for p in paths[:3]])


if __name__ == '__main__':
    unittest.main()
