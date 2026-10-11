import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'bmf_publication_dates', ROOT / 'crawler/sites/custom/bmf-gv-at-ministerium.py'
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
FIXTURES = ROOT / 'tests/fixtures/bmf_dates'


class BMFPublicationDateTests(unittest.TestCase):
    def test_strategy_year_in_actual_monitoring_document_is_not_publication(self):
        text = (FIXTURES / 'frontmatter-0.txt').read_text()
        self.assertIn('2021', text)
        self.assertIsNone(MODULE._extract_date(text, 'Monitoringkonzept.pdf'))

    def test_actual_progress_report_preserves_publication_month_and_raw_dateline(self):
        text = (FIXTURES / 'frontmatter-1.txt').read_text()
        evidence = MODULE._publication_date_evidence(text)
        self.assertEqual(evidence['value'], '2025-12')
        self.assertEqual(evidence['precision'], 'month')
        self.assertEqual(evidence['raw'], 'Wien, Dezember 2025')
        self.assertFalse(evidence['conflict'])

    def test_actual_evaluation_report_preserves_year_precision(self):
        text = (FIXTURES / 'frontmatter-2.txt').read_text()
        evidence = MODULE._publication_date_evidence(text)
        self.assertEqual(evidence['value'], '2026')
        self.assertEqual(evidence['precision'], 'year')

    def test_reporting_filename_and_revision_date_do_not_become_publication(self):
        self.assertIsNone(MODULE._extract_date('Stand: Dezember 2025', 'Bericht_2024.pdf'))
        self.assertIsNone(MODULE._extract_date('A cited event on 2024-03-01.', '2024.xlsx'))

    def test_explicit_days_are_validated_and_conflicting_datelines_retained(self):
        self.assertEqual(MODULE._extract_date('Wien, 29. Februar 2024'), '2024-02-29')
        self.assertIsNone(MODULE._extract_date('Wien, 29. Februar 2025'))
        evidence = MODULE._publication_date_evidence('Wien, 2024\nVienna, 2025')
        self.assertIsNone(evidence['value'])
        self.assertTrue(evidence['conflict'])
        self.assertEqual(len(evidence['candidates']), 2)


if __name__ == '__main__':
    unittest.main()
