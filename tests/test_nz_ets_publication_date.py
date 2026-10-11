import contextlib
import importlib.util
import io
import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from crawler import db, db_libertree, storage

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures/nz_ets_publication_date'
SPEC = importlib.util.spec_from_file_location(
    'nz_ets_date', ROOT / 'crawler/sites/custom/consult-environment-govt-nz-climate.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
Crawler = MODULE.ConsultEnvironmentGovtNzClimateCrawler


class NzEtsPublicationDateTests(unittest.TestCase):
    def setUp(self):
        self.sources = json.loads((FIXTURES / 'sources.json').read_text())['sources'][1:]
        self.before = json.loads((FIXTURES / 'before-papers.json').read_text())

    def parse(self, index, raw=None):
        source = self.sources[index]
        crawler = object.__new__(Crawler)
        with contextlib.redirect_stdout(io.StringIO()):
            return crawler._parse_respondent(
                raw if raw is not None else (FIXTURES / source['file']).read_text(),
                source['uid'], source['url'])

    def database(self, kind):
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        if kind == 'legacy':
            db.init_db(conn)
            db.register_site(conn, Crawler.site_id, Crawler.site_id, Crawler.base_url)
        else:
            db_libertree.init_db(conn)
            conn.execute('INSERT INTO sites(site_id,site_name,site_url) VALUES(?,?,?)',
                         (Crawler.site_id, Crawler.site_id, Crawler.base_url))
            conn.commit()
        return conn, Crawler(conn, delay=0)

    def test_actual_cohort_publication_date_is_separate_from_closing_date(self):
        source = BeautifulSoup((FIXTURES / 'consultation.html').read_text(), 'lxml')
        headings = [h.get_text(' ', strip=True) for h in source.select('h2')]
        self.assertIn('Published submissions - 19 August 2025', headings)
        self.assertIn('Closed 29 Jun 2025', source.get_text(' ', strip=True))
        paper = self.parse(0)
        self.assertEqual(paper['published_date'], '2025-08-19')
        self.assertEqual({k: v for k, v in paper.items() if k not in ('published_date', 'metadata')},
                         {k: v for k, v in self.before[0].items() if k not in ('published_date', 'metadata')})
        metadata = json.loads(paper['metadata'])
        for key, value in json.loads(self.before[0]['metadata']).items():
            self.assertEqual(metadata[key], value)
        self.assertEqual(metadata['consultation_closed'], '2025-06-29')
        self.assertEqual(metadata['publication_date_role'], 'published_submissions_cohort_date')

    def test_actual_short_PDF_submission_is_retained_without_padding_body(self):
        self.assertIsNone(self.before[1])
        paper = self.parse(1)
        self.assertEqual(paper['external_id'], '350025618')
        self.assertEqual(paper['title'], 'Z Energy')
        self.assertEqual(len(paper['abstract']), 32)
        self.assertEqual(paper['original_filename'], 'Z energy.pdf')
        source = BeautifulSoup((FIXTURES / 'response-1.html').read_text(), 'lxml')
        link = source.select_one('a[href*="download_public_attachment"]')
        self.assertEqual(paper['pdf_url'], link['href'])
        self.assertEqual(json.loads(paper['metadata'])['source_public_attachments'],
                         [{'url': link['href'], 'filename': 'Z energy.pdf'}])
        self.assertEqual(paper['published_date'], '2025-08-19')

    def test_short_response_needs_its_own_public_attachment(self):
        raw = (FIXTURES / 'response-1.html').read_text()
        for replacement in ['download_private_attachment', 'unrelated_attachment']:
            self.assertIsNone(self.parse(1, raw.replace('download_public_attachment', replacement)))
        self.assertIsNone(self.parse(1, raw.replace('uuId=350025618', 'uuId=123456789')))

    def test_new_dates_repeat_and_existing_identity_file_references_both_schemas(self):
        papers = [self.parse(0), self.parse(1)]
        for kind in ['legacy', 'v2']:
            with self.subTest(kind=kind), patch.object(
                    storage, 'cleanup_doc_files', side_effect=AssertionError('unexpected cleanup')):
                conn, crawler = self.database(kind)
                for paper in papers:
                    crawler._save_paper(paper)
                    self.assertTrue(crawler._last_save_created)
                rows = [dict(row) for row in conn.execute('SELECT * FROM documents')]
                self.assertEqual([row['published_date'] for row in rows], ['2025-08-19'] * 2)
                self.assertEqual(rows[1]['pdf_url'], papers[1]['pdf_url'])
                for paper in papers:
                    crawler._save_paper(paper)
                    self.assertFalse(crawler._last_save_created)
                self.assertEqual(rows, [dict(row) for row in conn.execute('SELECT * FROM documents')])
                prior_conn, prior_crawler = self.database(kind)
                prior_crawler._save_paper(self.before[0])
                if kind == 'legacy':
                    prior_conn.execute(
                        'UPDATE documents SET pdf_path=?,txt_path=?,summary=?',
                        ('/owned/audit-prior.pdf', '/owned/audit-prior.txt', 'retained summary'))
                    prior_conn.commit()
                old = dict(prior_conn.execute('SELECT * FROM documents').fetchone())
                prior_crawler._save_paper(papers[0])
                self.assertFalse(prior_crawler._last_save_created)
                after = dict(prior_conn.execute('SELECT * FROM documents').fetchone())
                if kind == 'legacy':
                    self.assertEqual(after['published_date'], '2025-08-19')
                    self.assertEqual(
                        {k: v for k, v in old.items() if k not in ('published_date', 'metadata')},
                        {k: v for k, v in after.items() if k not in ('published_date', 'metadata')})
                else:
                    # The existing v2 save path does not rewrite an existing row.
                    self.assertEqual(old, after)


if __name__ == '__main__':
    unittest.main()
