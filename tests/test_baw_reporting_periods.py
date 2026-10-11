import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'baw_reporting_periods', ROOT / 'crawler/sites/custom/baw-de-en.py'
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
FIXTURES = ROOT / 'tests/fixtures/baw_periods'


def replay():
    crawler = object.__new__(MODULE.BawDeEnCrawler)
    envelope = ET.fromstring((FIXTURES / 'annual-reports.xml').read_bytes())
    html = next(element.text for element in envelope.iter() if element.tag == 'body')
    items = crawler._parse_html_list(html)
    crawler._fetch_items = lambda: items
    crawler.detail_delay = 0
    # Test date/identity mapping for all actual list entries; these stand-ins
    # deliberately do not claim to verify every PDF or a database save.
    crawler._curl_get_bytes = lambda *args, **kwargs: b'PDF extraction stand-in'
    crawler._extract_pdf_text = lambda *args, **kwargs: (
        'Annual report content for the date mapping regression. ' * 4
    )
    rows = []

    def save(paper):
        rows.append(paper)
        crawler._last_save_created = True

    crawler._save_paper = save
    with contextlib.redirect_stdout(io.StringIO()), patch.object(
        MODULE.time, 'sleep', lambda _: None
    ):
        count = crawler.crawl()
    return count, rows, items


class BAWReportingPeriodTests(unittest.TestCase):
    def test_reporting_year_never_becomes_a_publication_date(self):
        count, rows, _ = replay()
        self.assertEqual(count, 14)
        for row in rows:
            self.assertIsNone(row['published_date'])
            self.assertIsNone(row['posted_date'])
            metadata = json.loads(row['metadata'])
            self.assertIsNone(metadata['posted_date'])
            self.assertEqual(metadata['year_role'], 'reporting_period')
            self.assertEqual(metadata['reporting_period_year_raw'], metadata['year'])
        self.assertEqual(json.loads(rows[0]['metadata'])['year'], '2024')

    def test_actual_fourteen_file_identities_and_periods_are_preserved(self):
        _, rows, items = replay()
        self.assertEqual([row['pdf_url'] for row in rows], [item['pdf_url'] for item in items])
        self.assertEqual([row['title'] for row in rows], [item['title'] for item in items])
        self.assertEqual([json.loads(row['metadata'])['year'] for row in rows],
                         [str(year) for year in range(2024, 2010, -1)])
        self.assertEqual(len({row['external_id'] for row in rows}), 14)
        for row in rows:
            self.assertEqual(row['external_id'], row['original_filename'][:-4])
            self.assertTrue(row['pdf_url'].endswith(row['original_filename']))


if __name__ == '__main__':
    unittest.main()
