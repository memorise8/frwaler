import importlib.util
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from crawler import db, storage
from crawler.base_crawler import CrawlCancelled

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('caisse_html_press', ROOT / 'crawler/sites/custom/caissedesdepots-fr-communiques-de-press.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
FIXTURE = ROOT / 'tests/fixtures/caisse_html_press'


class CaisseHtmlPressTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        db.init_db(self.conn)
        cls = MODULE.CaisseDesDepotsCommPresseCrawler
        db.register_site(self.conn, cls.site_id, cls.site_id, cls.base_url)
        self.crawler = cls(self.conn, delay=0)

    def items(self):
        return self.crawler._parse_page((FIXTURE / 'list-page0.html').read_text())

    def test_actual_HTML_identity_preserved_and_real_body_date_attachment_saved(self):
        before = json.loads((FIXTURE / 'before-first-page.json').read_text())
        items = self.items()
        self.assertEqual([r['pdf_href'] for r in before], [r['pdf_href'] for r in items])
        self.assertEqual([r['original_filename'] for r in before], [r['original_filename'] for r in items])
        self.assertEqual(items[0]['date_iso'], '2026-09-23')
        with patch.object(self.crawler, '_curl_get', return_value=(FIXTURE / 'first-detail.html').read_text()):
            paper = self.crawler._paper(items[0])
        self.assertEqual(paper['external_id'], before[0]['pdf_href'])
        self.assertEqual(paper['post_number'], before[0]['original_filename'])
        self.assertEqual(paper['url'], before[0]['pdf_url'])
        self.assertNotEqual(paper['pdf_url'], paper['url'])
        self.assertTrue(paper['pdf_url'].endswith('.pdf'))
        self.assertEqual(paper['published_date'], '2026-09-23')
        self.assertIn('Commission européenne', paper['abstract'])
        self.assertGreater(len(paper['abstract']), 5000)
        metadata = json.loads(paper['metadata'])
        self.assertEqual(metadata['abstract_source'], 'original HTML press body')
        self.assertEqual(metadata['publication_date_raw'], 'Date de publication 23 septembre 2026')
        self.assertEqual(metadata['source_pdf_urls'], [paper['pdf_url']])
        self.crawler._save_paper(paper)
        first = dict(self.conn.execute('SELECT * FROM documents').fetchone())
        self.crawler._save_paper(paper)
        self.assertFalse(self.crawler._last_save_created)
        self.assertEqual(first, dict(self.conn.execute('SELECT * FROM documents').fetchone()))

    def test_actual_source_pages_after200_follow_next_and_end_at255(self):
        base = self.crawler._LIST_URL
        lookup = {base+'?page='+str(n):(FIXTURE/f'list-page{n}.html').read_text() for n in [254,255]}
        with patch.object(self.crawler, '_curl_get', side_effect=lookup.__getitem__) as fetch:
            pages = list(self.crawler._iter_pages(base+'?page=254'))
        self.assertEqual([p[0] for p in pages], list(lookup))
        self.assertEqual([len(p[1]) for p in pages], [5,5])
        self.assertEqual(fetch.call_count, 2)

    def test_next_page_cannot_repeat_skip_or_leave_original_scope(self):
        raw = (FIXTURE / 'list-page254.html').read_text()
        for replacement in ['?page=254', '?page=256']:
            with self.assertRaises(ValueError):
                self.crawler._next_page(raw.replace('?page=255', replacement), self.crawler._LIST_URL+'?page=254')
        with self.assertRaises(ValueError):
            self.crawler._next_page('<a rel="next" href="https://example.org/?page=255">next</a>', self.crawler._LIST_URL+'?page=254')

    def test_missing_source_body_is_failure_not_fabricated_summary_or_exhaustion(self):
        item = self.items()[0]
        with patch.object(self.crawler, '_iter_pages', return_value=iter([(self.crawler._LIST_URL,[item])])), patch.object(self.crawler, '_curl_get', return_value='<main><h1 class="page__title">Error</h1></main>'):
            with self.assertRaises(RuntimeError):
                self.crawler.crawl()
        self.assertFalse(self.crawler.delivery_exhausted)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM documents').fetchone()[0], 0)

    def test_cancellation_propagates_and_zero_limit_makes_no_request(self):
        with patch.object(self.crawler, '_curl_get', side_effect=AssertionError('unexpected request')):
            self.assertEqual(self.crawler.crawl(limit=0), 0)
            self.crawler.delivery_should_cancel = lambda: True
            with self.assertRaises(CrawlCancelled):
                self.crawler.crawl()

    def test_cancel_after_actual_save_replay_preserves_stored_row(self):
        item = self.items()[0]
        with patch.object(self.crawler, '_iter_pages', side_effect=lambda:iter([(self.crawler._LIST_URL,[item])])), patch.object(self.crawler, '_curl_get', return_value=(FIXTURE / 'first-detail.html').read_text()):
            self.crawler.delivery_should_cancel = lambda:self.conn.execute('SELECT COUNT(*) FROM documents').fetchone()[0] >= 1
            with self.assertRaises(CrawlCancelled):
                self.crawler.crawl()
            before = dict(self.conn.execute('SELECT * FROM documents').fetchone())
            self.assertFalse(self.crawler.delivery_exhausted)
            self.crawler.delivery_should_cancel = lambda:False
            self.assertEqual(self.crawler.crawl(), 0)
            self.assertEqual(before, dict(self.conn.execute('SELECT * FROM documents').fetchone()))

    def test_invalid_and_partial_dates_not_stored_as_complete_publication(self):
        self.assertEqual(MODULE._parse_french_date('31 février 2026'), '')
        self.assertEqual(MODULE._parse_french_date('septembre 2026'), '')
        self.assertEqual(MODULE._parse_french_date('23 septembre 2026'), '2026-09-23')

    def test_actual_last_page_PDF_only_detail_retained_without_invented_body(self):
        item=self.crawler._parse_page((FIXTURE/'list-page255.html').read_text())[0]
        with patch.object(self.crawler,'_curl_get',return_value=(FIXTURE/'last-detail.html').read_text()):
            paper=self.crawler._paper(item)
        self.assertEqual(paper['abstract'],'')
        self.assertTrue(paper['pdf_url'].endswith('.pdf'))
        self.assertEqual(paper['external_id'],item['pdf_href'])
        self.assertIn('full text pending',json.loads(paper['metadata'])['abstract_source'])
        self.crawler._save_paper(paper)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM documents').fetchone()[0],1)

    def test_existing_wrong_attachment_is_held_without_deleting_files_or_rekeying(self):
        from tempfile import TemporaryDirectory
        item = self.items()[0]
        with patch.object(self.crawler, '_curl_get', return_value=(FIXTURE / 'first-detail.html').read_text()):
            paper = self.crawler._paper(item)
        old = dict(paper, pdf_url=paper['url'], summary='Existing summary')
        self.crawler._save_paper(old)
        with TemporaryDirectory() as directory:
            pdf = Path(directory) / 'retained.pdf'
            text = Path(directory) / 'retained.txt'
            pdf.write_bytes(b'owned historical file fixture')
            text.write_text('owned historical text fixture')
            self.conn.execute('UPDATE documents SET pdf_path=?,txt_path=?', (str(pdf),str(text)))
            self.conn.commit()
            before = dict(self.conn.execute('SELECT * FROM documents').fetchone())
            with patch.object(storage, 'cleanup_doc_files', side_effect=AssertionError('must not delete files')):
                with self.assertRaisesRegex(ValueError, 'preserve files'):
                    self.crawler._save_paper(paper)
            self.assertEqual(before, dict(self.conn.execute('SELECT * FROM documents').fetchone()))
            self.assertEqual(pdf.read_bytes(), b'owned historical file fixture')
            self.assertEqual(text.read_text(), 'owned historical text fixture')


if __name__ == '__main__':
    unittest.main()
