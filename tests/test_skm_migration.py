import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures/skm_migration'


class SkmMigration(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            'skm_migration', ROOT / 'crawler/sites/custom/skm-dk-aktuelt.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.crawler = object.__new__(self.module.SkmDkAktueltCrawler)
        self.press = json.loads((FIXTURES / 'press.json').read_text())
        self.report = json.loads((FIXTURES / 'report.json').read_text())

    def test_migrated_sitemap_includes_both_delivered_scopes(self):
        with patch.object(self.module, '_curl_get',
                          return_value=(FIXTURES / 'sitemap.xml').read_text()) as fetch:
            urls = self.crawler._get_sitemap_urls()
        fetch.assert_called_once_with('https://svmn.dk/sitemap.xml')
        self.assertEqual(len(urls), 2)
        self.assertIn('/pressemeddelelser/', urls[0])
        self.assertIn('/rapporter/', urls[1])

    def test_data_route_uses_path_for_current_and_legacy_origins(self):
        path = self.press['content']['page']['url']
        for origin in ['https://svmn.dk', 'https://skm.dk']:
            with self.subTest(origin=origin), patch.object(
                    self.module, '_curl_get', return_value=json.dumps({'pageProps': self.press})) as fetch:
                self.assertEqual(self.crawler._fetch_detail_json(origin + path, 'build-id'), self.press)
                fetch.assert_called_once_with('https://svmn.dk/_next/data/build-id' + path + '.json')

    def test_press_identity_date_and_body_survive_origin_migration(self):
        paper = self.crawler._parse_page_props(self.press)
        self.assertEqual(paper['external_id'], 'a1655e89-1cd9-4f7b-8a43-2f959502a758')
        self.assertEqual(paper['post_number'], paper['external_id'])
        self.assertEqual(paper['published_date'], '2023-09-12')
        self.assertEqual(len(paper['abstract']), 2636)
        self.assertTrue(paper['url'].startswith('https://svmn.dk/'))

    def test_report_structured_pdf_is_preserved_outside_body_markup(self):
        paper = self.crawler._parse_page_props(self.report)
        self.assertEqual(paper['external_id'], 'e32a35b6-8c77-44db-a2a5-90762d3212f4')
        media = self.report['content']['page']['properties']['pdf']
        self.assertEqual(paper['pdf_url'], 'https://svmn.dk' + media[0]['url'])
        self.assertEqual(paper['original_filename'], 'groen-skattereform-endelig-afrapportering-tilgaengeliggjort.pdf')
        self.assertEqual(json.loads(paper['metadata'])['publication_files'], media)
        self.assertEqual(paper['published_date'], '2024-02-21')


if __name__ == '__main__':
    unittest.main()
