import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from crawler.libertree_adapter import paper_to_document

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'astron_reporting_periods', ROOT / 'crawler/sites/custom/astron-nl-about.py'
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
FIXTURES = ROOT / 'tests/fixtures/astron_periods'


def replay():
    crawler = object.__new__(MODULE.AstronNlAboutCrawler)
    crawler._delay = 0
    crawler._curl = lambda url: (FIXTURES / 'annual-cards.html').read_text()
    rows = []

    def save(paper):
        rows.append(paper)
        crawler._last_save_created = True

    crawler._save_paper = save
    with patch.object(MODULE.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()):
        count = crawler.crawl()
    return count, rows


class AstronReportingPeriodTests(unittest.TestCase):
    def test_actual_fifty_seven_periods_do_not_become_december_31(self):
        count, rows = replay()
        self.assertEqual(count, 57)
        for row in rows:
            self.assertIsNone(row['published_date'])
            metadata = json.loads(row['metadata'])
            self.assertEqual(metadata['year_role'], 'reporting_period')
            self.assertEqual(metadata['reporting_period_raw'], metadata['title_year'])
        periods = {json.loads(row['metadata'])['reporting_period_raw'] for row in rows}
        self.assertIn('2003 - 2004', periods)
        self.assertIn('2005 - 2006', periods)

    def test_normal_and_extra_edition_keep_distinct_file_based_storage_identity(self):
        count, rows = replay()
        documents = [paper_to_document(row) for row in rows]
        self.assertEqual(len({doc['external_id'] for doc in documents}), 57)
        self.assertEqual(len({doc['pdf_url'] for doc in documents}), 57)
        editions = [doc for doc in documents if '1979' in doc['title']]
        self.assertEqual(len(editions), 2)
        self.assertNotEqual(editions[0]['external_id'], editions[1]['external_id'])
        self.assertTrue(any('Extra Edition' in doc['title'] for doc in editions))
        self.assertEqual(documents[0]['external_id'], 'ASTRON_Annual_Report_2013')
        self.assertTrue(documents[0]['pdf_url'].endswith('ASTRON_Annual_Report_2013.pdf'))


if __name__ == '__main__':
    unittest.main()
