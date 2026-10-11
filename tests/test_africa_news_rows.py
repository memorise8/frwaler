import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('africa_news_rows', ROOT / 'crawler/sites/custom/africamuseum-be-en.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
CLS = next(c for c in vars(MODULE).values() if isinstance(c, type) and getattr(c, 'site_id', None) == 'africamuseum-be-en')
FIXTURE = (ROOT / 'tests/fixtures/africa_news/source_image_text_pairs.html').read_text()


class AfricaNewsRowsTests(unittest.TestCase):
    def setUp(self):
        self.crawler = object.__new__(CLS)

    def test_source_image_and_text_rows_preserve_dates_and_identity(self):
        rows = self.crawler._parse_list_page(FIXTURE)
        self.assertEqual([r[0] for r in rows], ['PROMA', 'Erfgoedchallenge'])
        self.assertEqual([r[2] for r in rows], ['2026-10', '2026-09'])
        self.assertTrue(rows[0][1].startswith('CARTELS'))
        self.assertIn('What does the museum show', rows[0][3])

    def test_reversed_duplicate_rows_keep_same_fields(self):
        soup = MODULE._make_soup(FIXTURE)
        reordered = ''.join(str(r) for r in reversed(soup.select('.views-row')))
        expected = {r[0]: r for r in self.crawler._parse_list_page(FIXTURE)}
        self.assertEqual({r[0]: r for r in self.crawler._parse_list_page(reordered)}, expected)

    def test_image_only_destination_is_still_retained(self):
        soup = MODULE._make_soup(FIXTURE)
        rows = self.crawler._parse_list_page(str(soup.select_one('.views-row')))
        self.assertEqual(rows, [('PROMA', '', None, '', 'https://www.africamuseum.be/en/research/news/PROMA')])

    def test_source_historical_paths_and_encoded_slug_remain_exact(self):
        raw = (ROOT / 'tests/fixtures/africa_news/source_old_paths.html').read_text()
        rows = self.crawler._parse_list_page(raw)
        self.assertEqual([r[0] for r in rows], ['Women%27sDay', 'pasteca', 'atrap'])
        self.assertEqual([r[4] for r in rows], [
            'https://www.africamuseum.be/en/research/news/Women%27sDay',
            'https://www.africamuseum.be/en/news/pasteca',
            'https://www.africamuseum.be/en/research/discover/news/atrap',
        ])

    def test_detail_retains_source_path_and_node_identity(self):
        html = '<h1>Study</h1><div data-history-node-id="123"></div><div class="field--name-field-description">' + 'Research findings. ' * 20 + '</div>'
        url = 'https://www.africamuseum.be/en/research/discover/news/atrap'
        paper = self.crawler._parse_detail_page(html, 'atrap', '2021-04', source_url=url)
        self.assertEqual(paper['url'], url)
        self.assertEqual(paper['external_id'], '123')
        self.assertEqual(paper['post_number'], '123')
        self.assertEqual(paper['published_date'], '2021-04')


if __name__ == '__main__':
    unittest.main()
