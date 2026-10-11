import importlib.util
from pathlib import Path
import unittest
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('afp_search_cards', ROOT / 'crawler/sites/custom/afp-gov-au-search.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
FIXTURE = (ROOT / 'tests/fixtures/afp_search/source_mixed_cards.html').read_text()

class AFPSearchCardsTests(unittest.TestCase):
    def test_actual_mixed_cards_include_all_twelve_destinations(self):
        rows = MODULE._parse_list_page(FIXTURE)
        self.assertEqual(len(rows), 12)
        self.assertEqual(len({r['url'] for r in rows}), 12)
        self.assertEqual(rows[0]['node_id'], '208')
        self.assertEqual(rows[0]['title'], 'Information Publication Scheme')
        self.assertEqual(rows[0]['url'], 'https://afp.gov.au/about-us/governance-and-accountability/ips')
        self.assertEqual(rows[0]['listed_date'], '2023-07-10')
        self.assertEqual(rows[1]['node_id'], '221')
        self.assertTrue(all(r['title'] != 'Read this' for r in rows))

    def test_original_title_link_card_retains_fields(self):
        rows = MODULE._parse_list_page(FIXTURE)
        item = next(r for r in rows if r['title'] == 'Espionage and foreign interference')
        self.assertEqual(item['node_id'], '')
        self.assertEqual(item['url'], 'https://afp.gov.au/crimes/espionage-and-foreign-interference')
        self.assertEqual(item['listed_date'], '')
        self.assertEqual(item['datetime_raw'], '')
        self.assertIn('Foreign', item['snippet'])

    def test_missing_destination_is_skipped_without_losing_next_card(self):
        soup = BeautifulSoup(FIXTURE, 'html.parser')
        first = soup.select_one('.views-row')
        for a in first.select('a[href]'):
            a.decompose()
        rows = MODULE._parse_list_page(str(soup))
        self.assertEqual(len(rows), 11)
        self.assertEqual(rows[0]['node_id'], '221')

if __name__ == '__main__':
    unittest.main()
