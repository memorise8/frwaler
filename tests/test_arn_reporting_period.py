import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'arn_reporting_period', ROOT / 'crawler/sites/custom/arn-se-om-arn.py'
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
FIXTURES = ROOT / 'tests/fixtures/arn_dates'


def replay():
    crawler = object.__new__(MODULE.ArnSeOmArnCrawler)
    crawler.delivery_should_cancel = lambda: False
    crawler._curl_get = lambda url: (FIXTURES / 'annual-list.html').read_text()
    rows = []

    def save(paper):
        rows.append(paper)
        crawler._last_save_created = True

    crawler._save_paper = save
    with contextlib.redirect_stdout(io.StringIO()):
        count = crawler.crawl()
    return count, rows


class ARNReportingPeriodTests(unittest.TestCase):
    def test_reporting_year_does_not_become_publication_or_listing_date(self):
        count, rows = replay()
        self.assertEqual(count, 12)
        for row in rows:
            self.assertIsNone(row['published_date'])
            self.assertIsNone(row['listed_date'])
            metadata = json.loads(row['metadata'])
            self.assertIsNone(metadata['posted_date'])
            self.assertEqual(metadata['year_role'], 'reporting_period')
            self.assertEqual(metadata['reporting_period_year_raw'], row['external_id'])

    def test_actual_twelve_reports_keep_identity_period_and_file_references(self):
        count, rows = replay()
        expected = [str(year) for year in range(2025, 2013, -1)]
        self.assertEqual([row['external_id'] for row in rows], expected)
        self.assertEqual([row['post_number'] for row in rows], expected)
        self.assertEqual([json.loads(row['metadata'])['year'] for row in rows], expected)
        self.assertEqual(len({row['pdf_url'] for row in rows}), 12)
        self.assertEqual(rows[0]['pdf_url'],
                         'https://www.arn.se/globalassets/extern/arsredovisningar/'
                         'allmanna-reklamationsnamndens-arsredovisning-2025.pdf')
        self.assertTrue(all(row['pdf_url'].endswith(row['original_filename']) for row in rows))


if __name__ == '__main__':
    unittest.main()
