import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from pypdf import PdfWriter
from crawler import db
from crawler.base_crawler import CrawlCancelled

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'budget_pdf_only', ROOT / 'crawler/sites/custom/budget-canada-ca-home-accueil-enhtml.py'
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
FIXTURE = ROOT / 'tests/fixtures/budget_pdf_only'
CATALOGUE = json.loads((FIXTURE / 'source-records.json').read_text())['data']
LIST_URL = 'https://budget.canada.ca/budget.json'


class BudgetPdfOnlyTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        db.init_db(self.conn)
        sid = MODULE.BudgetCanadaCaHomeAccueilEnhtmlCrawler.site_id
        db.register_site(self.conn, sid, sid, 'https://budget.canada.ca')
        self.crawler = MODULE.BudgetCanadaCaHomeAccueilEnhtmlCrawler(self.conn, delay=0)

    def record(self, year):
        return next(x for x in CATALOGUE if x['date'].startswith(str(year)))

    def run_records(self, records, pdf_bytes=None):
        def html(url, **kwargs):
            if url == self.crawler.START_URL:
                return (FIXTURE / 'source-entry.html').read_text()
            if url == 'https://budget.canada.ca/archives.js':
                return (FIXTURE / 'archives.js').read_text()
            if url == 'https://budget.canada.ca/2025/home-accueil-en.html':
                return (FIXTURE / '2025-detail.html').read_text()
            self.fail(f'unexpected HTML URL: {url}')

        def pdf(url):
            if pdf_bytes is not None:
                return pdf_bytes
            if url.endswith('/bp2006e.pdf'):
                return (FIXTURE / '2006-first-two-pages.pdf').read_bytes()
            if url.endswith('/1968-sd-eng.pdf'):
                return (FIXTURE / '1968-first-two-pages.pdf').read_bytes()
            self.fail(f'unexpected PDF URL: {url}')

        with patch.object(self.crawler, '_curl_get', side_effect=html), \
                patch.object(self.crawler, '_curl_json', return_value={'data': copy.deepcopy(records)}), \
                patch.object(self.crawler, '_curl_pdf', side_effect=pdf), \
                contextlib.redirect_stdout(io.StringIO()):
            return self.crawler.crawl()

    def test_all_59_original_list_identities_and_HTML_detail_fields_are_unchanged(self):
        expected = json.loads((FIXTURE / 'expected-list-records.json').read_text())
        actual = [self.crawler._parse_list_record(x, i, LIST_URL)
                  for i, x in enumerate(CATALOGUE, start=1)]
        self.assertEqual(actual, expected)
        self.assertEqual(sum(not x['url'] and bool(x['pdf_url']) for x in actual), 40)
        html = self.crawler._make_soup((FIXTURE / '2025-detail.html').read_text())
        self.assertEqual(self.crawler._parse_detail(html, actual[0]),
                         json.loads((FIXTURE / 'expected-2025-detail.json').read_text()))

    def test_actual_HTML_and_PDF_only_records_save_and_repeat_without_duplicates(self):
        records = [self.record(2025), self.record(2006), self.record(1968)]
        self.assertEqual(self.run_records(records), 3)
        before = [dict(r) for r in self.conn.execute('SELECT * FROM documents ORDER BY id')]
        self.assertEqual(self.run_records(records), 0)
        self.assertEqual(before, [dict(r) for r in self.conn.execute('SELECT * FROM documents ORDER BY id')])
        for row in before[1:]:
            metadata = json.loads(row['metadata'])
            self.assertEqual(row['meta_url'], row['pdf_url'])
            self.assertEqual(metadata['source_format'], 'json+pdf')
            self.assertEqual(metadata['pdf_pages'], 2)  # source-derived page subsets
            self.assertFalse(metadata['ocr_performed'])
            self.assertGreater(len(row['abstract']), 50)
        self.assertEqual(before[-1]['published_date'], '1968-10-22')
        self.assertIn('2011', json.loads(before[-1]['metadata'])['pdf_creation_date_raw'])

    def test_valid_PDF_without_text_is_retained_without_inventing_an_abstract(self):
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=400)
        stream = io.BytesIO()
        writer.write(stream)
        self.assertEqual(self.run_records([self.record(2006)], stream.getvalue()), 1)
        row = dict(self.conn.execute('SELECT * FROM documents').fetchone())
        self.assertEqual(row['abstract'], '')
        metadata = json.loads(row['metadata'])
        self.assertEqual(metadata['pdf_text_status'], 'no_text_layer')
        self.assertFalse(metadata['ocr_performed'])
        self.assertEqual(row['published_date'], '2006-05-02')

    def test_non_PDF_response_cannot_be_saved_as_an_archived_budget(self):
        self.assertEqual(self.run_records([self.record(2006)], b'<html>Access denied</html>'), 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM documents').fetchone()[0], 0)

    def test_cancellation_is_not_swallowed_by_PDF_parsing(self):
        self.crawler.delivery_should_cancel = lambda: True
        record = self.crawler._parse_list_record(self.record(1968), 1, LIST_URL)
        with self.assertRaises(CrawlCancelled):
            self.crawler._parse_pdf_record(record, (FIXTURE / '1968-first-two-pages.pdf').read_bytes())
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM documents').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
