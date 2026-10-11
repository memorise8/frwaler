import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from crawler.document_dates import normalize_date

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('ami_date_precision', ROOT / 'crawler/sites/custom/ami-swiss-en.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
CLS = next(c for c in vars(MODULE).values() if isinstance(c, type) and getattr(c, 'site_id', None) == 'ami-swiss-en')
FIXTURES = ROOT / 'tests/fixtures/ami_dates'


def replay(index, year, message=None):
    crawler = object.__new__(CLS)
    crawler._delay = 0
    crawler._get_years = lambda: [year]
    crawler._curl_get = lambda url: (FIXTURES / f'list-{index}.html').read_text()
    message = json.loads((FIXTURES / f'crossref-{index}.json').read_text()) if message is None else message
    crawler._fetch_crossref = lambda doi: message
    rows = []

    def save(paper):
        rows.append(paper)
        crawler._last_save_created = True

    crawler._save_paper = save
    with contextlib.redirect_stdout(io.StringIO()), patch.object(MODULE.time, 'sleep'):
        count = crawler.crawl()
    return count, rows


class AMIDatePrecisionTests(unittest.TestCase):
    def test_actual_complete_publication_date_and_source_year_have_distinct_precision(self):
        count, rows = replay(0, '2026')
        self.assertEqual(count, 1)
        row = rows[0]
        self.assertEqual(row['published_date'], '2026-10-05')
        self.assertEqual(row['listed_date'], '2026')
        self.assertEqual(row['external_id'], '10.1021/acsphotonics.6c01278')
        self.assertEqual(row['post_number'], row['external_id'])
        metadata = json.loads(row['metadata'])
        self.assertEqual(metadata['source_publication_year_raw'], '2026')
        self.assertEqual(metadata['crossref_dates_raw']['published']['date-parts'], [[2026, 10, 5]])
        self.assertFalse(metadata['publication_year_conflict'])

    def test_actual_year_only_publication_never_gains_january_first(self):
        count, rows = replay(2, '2024')
        self.assertEqual(count, 1)
        for field in ('published_date', 'listed_date'):
            self.assertEqual(rows[0][field], '2024')
            result = normalize_date(rows[0][field])
            self.assertIsNone(result['value'])
            self.assertEqual(result['precision'], 'year')
            self.assertEqual(result['raw'], '2024')

    def test_month_only_date_keeps_month_precision(self):
        value = CLS._crossref_date({'published': {'date-parts': [[2024, 5]]}})
        self.assertEqual(value, '2024-05')
        self.assertEqual(normalize_date(value)['precision'], 'month')

    def test_registry_creation_does_not_replace_publication_year(self):
        message = json.loads((FIXTURES / 'crossref-2.json').read_text())
        for key in ('published', 'published-print', 'published-online', 'issued'):
            message.pop(key, None)
        count, rows = replay(2, '2024', message)
        self.assertEqual(count, 1)
        self.assertEqual(rows[0]['published_date'], '2024')
        metadata = json.loads(rows[0]['metadata'])
        self.assertEqual(metadata['published_date_source'], 'source_publication_year')
        self.assertEqual(metadata['crossref_dates_raw']['created'], message['created'])

    def test_actual_old_record_without_abstract_remains_an_explicit_exclusion(self):
        message = json.loads((FIXTURES / 'crossref-1.json').read_text())
        self.assertEqual(CLS._crossref_date(message), '2014')
        count, rows = replay(1, '2008')
        self.assertEqual((count, rows), (0, []))

    def test_conflicting_source_years_are_retained_without_rekeying(self):
        count, rows = replay(2, '2023')
        self.assertEqual(count, 1)
        self.assertEqual(rows[0]['listed_date'], '2023')
        self.assertEqual(rows[0]['published_date'], '2024')
        self.assertTrue(json.loads(rows[0]['metadata'])['publication_year_conflict'])
        self.assertEqual(rows[0]['external_id'], '10.1039/d4tc01010h')


if __name__ == '__main__':
    unittest.main()
