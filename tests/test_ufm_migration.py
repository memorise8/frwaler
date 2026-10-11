import importlib.util
from pathlib import Path
import unittest
ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('ufm_migration', ROOT / 'crawler/sites/custom/ufm-dk-publikationer.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
FIXTURE = ROOT / 'tests/fixtures/ufm_migration'

class UFMMigrationTests(unittest.TestCase):
    def setUp(self):
        self.crawler = object.__new__(MODULE.UfmDkPublikationerCrawler)

    def test_current_archive_and_actual_publication_links(self):
        self.assertEqual(self.crawler._LIST_BASE, 'https://fudm.dk/publikationer/')
        links = self.crawler._parse_list_page((FIXTURE / 'list.html').read_text())
        self.assertEqual(len(links), 10)
        self.assertEqual(len(set(links)), 10)
        self.assertTrue(all(u.startswith('https://fudm.dk/publikationer/') for u in links))

    def test_relative_pdf_uses_current_origin_preserving_document_identity(self):
        url = 'https://fudm.dk/publikationer/2026/august/et-glansbillede-af-gennemsigtighed/'
        paper = self.crawler._parse_detail(url, (FIXTURE / 'detail.html').read_text())
        self.assertEqual(paper['site_id'], 'ufm-dk-publikationer')
        self.assertEqual(paper['external_id'], 'et-glansbillede-af-gennemsigtighed')
        self.assertEqual(paper['post_number'], '39084')
        self.assertEqual(paper['published_date'], '2026-08-31')
        self.assertEqual(paper['title'], 'Et glansbillede af gennemsigtighed')
        self.assertEqual(paper['pdf_url'], 'https://fudm.dk/media/wodcr3tp/et-glansbillede-af-gennemsigtighed.pdf')
        self.assertGreater(len(paper['abstract']), 100)

if __name__ == '__main__':
    unittest.main()
