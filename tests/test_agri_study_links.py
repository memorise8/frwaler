import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('agri_study_links', ROOT / 'crawler/sites/custom/agri-ee-ministeerium-uudised.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
CLS = next(c for c in vars(MODULE).values() if isinstance(c, type) and getattr(c, 'site_id', None) == 'agri-ee-ministeerium-uudised')
ROWS = json.loads((ROOT / 'tests/fixtures/agri_studies/source_nested_link_rows.json').read_text())


class AgriStudyLinksTests(unittest.TestCase):
    def setUp(self):
        self.crawler = object.__new__(CLS)

    def test_actual_nested_file_link_survives_all_parser_repairs(self):
        results = []
        for parser in ('html5lib', 'lxml', 'html.parser'):
            with patch.object(self.crawler, '_make_soup', side_effect=lambda raw, **kw: BeautifulSoup(raw, parser)):
                results.append(self.crawler._parse_row(ROWS[0], 'source-table', 1, {}))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], results[2])
        row = results[0]
        self.assertTrue(row['item_url'].endswith('%282026%29.pdf'))
        self.assertEqual(row['item_url'], row['pdf_url'])
        self.assertEqual(row['listed_date'], '2026-10-05')
        self.assertEqual(row['listed_datetime_raw'], '2026-10-05T10:43:17+03:00')
        self.assertEqual(row['file_size'], '4.69 MB')
        self.assertEqual(row['row_html'], ROWS[0])

    def test_plain_file_retains_its_original_destination(self):
        row = self.crawler._parse_row(ROWS[1], 'source-table', 2, {})
        link = BeautifulSoup(ROWS[1][0], 'html5lib').find('a', href=True)
        self.assertEqual(row['item_url'], link['href'])
        self.assertEqual(row['title'], ' '.join(link.get_text(' ', strip=True).split()))


if __name__ == '__main__':
    unittest.main()
