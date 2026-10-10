"""Regressions from retained FSS and GICHD public source responses."""
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / 'fixtures/source_route_migrations'


def load(site_id):
    spec = importlib.util.spec_from_file_location(site_id.replace('-', '_'),
                                                ROOT / 'crawler/sites/custom' / (site_id + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cls = next(value for value in vars(module).values()
               if isinstance(value, type) and getattr(value, 'site_id', None) == site_id)
    return object.__new__(cls)


class SourceRouteMigrations(unittest.TestCase):
    def test_fss_current_table_preserves_source_metadata_and_document_id(self):
        crawler = load('fss-or-kr-fss')
        raw = (FIXTURES / 'fss-krds-list.html').read_text()
        rows = crawler._parse_list_rows(raw)
        self.assertEqual(len(rows), 10)
        first = rows[0]
        self.assertEqual(first['ntt_id'], '238317')
        self.assertEqual(first['num'], '20920')
        self.assertEqual(first['department'], '소비자권익보호국/중소금융감독국')
        self.assertEqual(first['date'], '2026-10-08')
        self.assertIn('nttId=238317', first['url'])
        self.assertTrue(all(row['date'] and row['department'] and row['num'] for row in rows))

    def test_fss_legacy_table_still_parses(self):
        crawler = load('fss-or-kr-fss')
        soup = BeautifulSoup((FIXTURES / 'fss-krds-list.html').read_text(), 'lxml')
        table = soup.find('table')
        table['class'] = []
        rows = crawler._parse_list_rows('<div class="bd-list">' + str(table) + '</div>')
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0]['ntt_id'], '238317')
        self.assertEqual(rows[0]['date'], '2026-10-08')

    def test_gichd_current_source_list_is_not_empty(self):
        crawler = load('gichd-org-publications-resourc')
        raw = (FIXTURES / 'gichd-new-route-list.html').read_text()
        with patch.object(crawler, '_curl_get', return_value=raw):
            rows, has_next = crawler._fetch_list_page(1)
        self.assertEqual(len(rows), 10)
        self.assertTrue(has_next)
        self.assertEqual(rows[0]['slug'],
                         'improving-operational-effectiveness-in-colombia-through-community-informed-approaches')
        self.assertEqual(rows[0]['raw_date'], '28/09/2026')
        self.assertTrue(all('/publications-and-reports/' in row['url'] for row in rows))

    def test_gichd_routes_keep_slug_identity_and_reject_non_documents(self):
        crawler = load('gichd-org-publications-resourc')
        for route in ('publications', 'publications-and-reports'):
            prefix = '/publications-resources/' + route + '/'
            url = crawler._canonical_publication_url(prefix + 'land-release-research-agenda/')
            self.assertEqual(crawler._slug_from_url(url), 'land-release-research-agenda')
            for suffix in ('', '1/', '114/', 'rss/'):
                self.assertIsNone(crawler._canonical_publication_url(prefix + suffix))
        self.assertIsNone(crawler._canonical_publication_url('https://example.org/publications-resources/publications-and-reports/report/'))
        self.assertIsNone(crawler._canonical_publication_url('/news/report/'))
        self.assertEqual(crawler._page_url(2), 'https://www.gichd.org/publications-resources/publications-and-reports/1/')


if __name__ == '__main__':
    unittest.main()
