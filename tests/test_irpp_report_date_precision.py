"""The retained IRPP index supplies report years, not September publication days."""
import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / 'fixtures/irpp_annual_reports/year-only-cards.html'


def crawler():
    spec = importlib.util.spec_from_file_location('irpp_annual', ROOT / 'crawler/sites/custom/irpp-org-about-the-irpp.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return object.__new__(module.IRPPOrgAboutTheIRPPCrawler)


class ReportDatePrecision(unittest.TestCase):
    def test_all_retained_report_years_remain_raw_without_a_fabricated_day(self):
        rows = crawler()._parse_reports(FIXTURE.read_text())
        self.assertEqual(len(rows), 26)
        self.assertEqual([x['year_label'] for x in rows], [str(y) for y in range(2026, 2000, -1)])
        self.assertTrue(all(x['published_date'] is None for x in rows))
        self.assertTrue(all(x['post_number'] == x['year_label'] for x in rows))
        self.assertEqual(rows[0]['external_id'], 'Annual-Report-IRPP-2025-26')
        self.assertEqual(rows[-1]['external_id'], 'annrpt011')

    def test_crawl_passes_null_dates_and_original_year_identity_to_save(self):
        obj = crawler()
        obj._delay = 0
        obj._last_save_created = True
        saved = []
        with patch.object(obj, '_curl_get', return_value=FIXTURE.read_text()), \
             patch.object(obj, '_extract_abstract_from_pdf', return_value='Retained PDF text fixture. ' * 10), \
             patch.object(obj, '_save_paper', side_effect=saved.append):
            obj.crawl(limit=1)
        self.assertEqual(len(saved), 1)
        paper = saved[0]
        self.assertIsNone(paper['published_date'])
        self.assertIsNone(paper['posted_date'])
        self.assertEqual(paper['external_id'], 'Annual-Report-IRPP-2025-26')
        self.assertEqual(paper['post_number'], '2026')
        self.assertEqual(paper['original_filename'], 'Annual-Report-IRPP-2025-26.pdf')
        self.assertEqual(json.loads(paper['metadata'])['year_label'], '2026')


if __name__ == '__main__':
    unittest.main()
