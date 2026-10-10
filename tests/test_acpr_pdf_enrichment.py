"""PDF-only ACPR notices: real PDF extraction, native saves and cursor holds."""
import hashlib
import io
import json
import os
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import requests
from pypdf import PdfReader, PdfWriter
from pypdf.constants import UserAccessPermissions

from crawler.base_crawler import CrawlCancelled
from tests.test_delivery_site_new_counts import SiteContract
from tests.test_acpr_delivery_scopes import detail, listing, PATHS
from tests.test_onera_pdf_enrichment import pdf_bytes


class AcprPdfEnrichment(SiteContract, unittest.TestCase):
    SITE = 'acpr-banque-france-fr-fr'

    def crawler(self):
        c = self.configured(self.module.ACPRBanqueFranceCrawler(self.conn, delay=0))
        self.addCleanup(c._session.close)
        def html(url):
            parsed = urlsplit(url)
            for scope, path in PATHS.items():
                if parsed.path == path:
                    return listing(scope, int(parse_qs(parsed.query)['page'][0]))
            uid = parsed.path.rsplit('/', 1)[-1]
            return detail(uid, body='') if uid in ('s1', 's4') else detail(uid)
        c._curl_get = Mock(side_effect=html)
        def pdf(url, **kwargs):
            self.assertFalse(kwargs['allow_redirects'])
            uid = urlsplit(url).path.rsplit('/', 1)[-1][:-4]
            body = getattr(self, 'pdf_override', pdf_bytes(int(uid[1:])))
            response = requests.Response()
            response.url = url
            response.status_code = getattr(self, 'pdf_status', 200)
            response.raw = io.BytesIO(body)
            response.headers['Content-Length'] = str(getattr(self, 'content_length', len(body)))
            if response.status_code == 302:
                response.headers['Location'] = 'https://outside.invalid/report.pdf'
            return response
        c._session.get = self.pdf_get = Mock(side_effect=pdf)
        return c

    def test_pdf_body_identity_provenance_and_no_file_flags(self):
        c = self.crawler()
        papers = {}
        save = c._save_paper
        def record(p):
            papers[p['external_id']] = dict(p)
            return save(p)
        c._save_paper = record
        self.assertEqual(c.crawl(), 8)
        self.assertTrue(c.delivery_exhausted)
        self.assertEqual(self.pdf_get.call_count, 2)
        for r in self.conn.execute('SELECT post_number,abstract,pdf_downloaded,text_extracted FROM documents'):
            row = dict(r) if isinstance(r, dict) else dict(zip(('post_number','abstract','pdf_downloaded','text_extracted'), r))
            self.assertFalse(row['pdf_downloaded'])
            self.assertFalse(row['text_extracted'])
            if row['post_number'] in ('s1', 's4'):
                self.assertIn('Original publisher aerospace', row['abstract'])
                meta = json.loads(papers[row['post_number']]['metadata'])
                original = pdf_bytes(int(row['post_number'][1:]))
                self.assertEqual(meta['abstract_source'], 'publisher_pdf_text_excerpt')
                self.assertEqual(meta['abstract_pdf_sha256'], hashlib.sha256(original).hexdigest())
                self.assertEqual(meta['abstract_pdf_bytes'], len(original))
                self.assertEqual(len(meta['abstract_pdf_text_sha256']), 64)
                self.assertEqual(meta['source_pdf_urls'], [meta['abstract_pdf_url']])

    def test_denial_and_rate_limit_preserve_first_cursor(self):
        for status, reason in [(401,'authentication_failed'),(403,'authentication_failed'),(429,'rate_limited')]:
            self.pdf_status = status
            c = self.crawler(); c.delivery_mode = 'backfill'
            self.assertEqual(c.crawl(), 0)
            self.assertEqual(c.delivery_source_failure, reason)
            self.assertEqual(c.delivery_pending_cursor['offset'], 0)
            self.assertFalse(c.delivery_exhausted)
            self.assertEqual(self.pdf_get.call_count, 1)

    def test_missing_pdf_redirect_or_server_failure_is_not_complete(self):
        for status in (302,404,500,503):
            self.pdf_status = status; c = self.crawler()
            self.assertEqual(c.crawl(), 0)
            self.assertEqual(c.delivery_source_failure, 'fetch_failed')
            self.assertFalse(c.delivery_exhausted)
            self.assertEqual(self.pdf_get.call_count, 1)

    def test_pdf_transport_failures_stop_before_later_documents(self):
        for mode in ('full', 'backfill'):
            for error in (requests.ConnectionError('connection interrupted'), requests.ReadTimeout('read timeout')):
                c = self.crawler(); c.delivery_mode = mode
                self.pdf_get.side_effect = error
                self.assertEqual(c.crawl(), 0)
                self.assertEqual(c.delivery_source_failure, 'fetch_failed')
                self.assertFalse(c.delivery_exhausted)
                self.assertEqual(self.pdf_get.call_count, 1)
                if mode == 'backfill': self.assertEqual(c.delivery_pending_cursor['offset'], 0)

    def test_unreviewed_pdf_targets_make_no_request(self):
        c = self.crawler()
        for url in ('http://acpr.banque-france.fr/system/files/x.pdf', 'https://outside.invalid/x.pdf',
                    'https://acpr.banque-france.fr:443/system/files/x.pdf',
                    'https://acpr.banque-france.fr/elsewhere/x.pdf',
                    'https://acpr.banque-france.fr/system/files/x.pdf?other=1'):
            with self.assertRaises(ValueError): c._abstract_from_pdf(url)
        self.pdf_get.assert_not_called()

    def test_non_pdf_response_and_declared_truncation_hold_cursor(self):
        for body, length in [(b'<html>Publisher error</html>', 28), (pdf_bytes(1), len(pdf_bytes(1))+1)]:
            self.pdf_override = body; self.content_length = length
            c = self.crawler(); c.delivery_mode = 'backfill'
            self.assertEqual(c.crawl(), 0)
            self.assertEqual(c.delivery_source_failure, 'item_failed')
            self.assertEqual(c.delivery_pending_cursor['offset'], 0)
            self.assertEqual(self.count(), 0)

    def test_declared_and_streamed_size_budgets_hold_cursor(self):
        c = self.crawler(); c.delivery_mode = 'backfill'
        self.content_length = 21*1024**2
        self.assertEqual(c.crawl(), 0)
        self.assertEqual(c.delivery_source_failure, 'item_failed')
        self.content_length = 0
        c = self.crawler(); c.delivery_mode = 'backfill'
        with patch.object(self.module, '_PDF_MAX_BYTES', 100): self.assertEqual(c.crawl(), 0)
        self.assertEqual(c.delivery_pending_cursor['offset'], 0)
        self.assertEqual(self.count(), 0)

    def test_download_wall_budget_and_text_timeout_hold_cursor(self):
        c = self.crawler()
        with patch.object(self.module.time, 'monotonic', side_effect=[0,181]):
            with self.assertRaisesRegex(ValueError, 'download budget exceeded'):
                c._abstract_from_pdf('https://acpr.banque-france.fr/system/files/s1.pdf')
        self.assertEqual(self.pdf_get.call_count, 1)
        c = self.crawler(); c.delivery_mode = 'backfill'
        with patch('delivery.files.worker.run_stage', return_value={'success':False,'error':'stage_timeout'}):
            self.assertEqual(c.crawl(), 0)
        self.assertEqual(c.delivery_source_failure, 'item_failed')
        self.assertEqual(self.count(), 0)

    def test_image_and_copy_restricted_pdfs_do_not_fabricate_body(self):
        blank = PdfWriter(); blank.add_blank_page(width=300, height=200)
        restricted = PdfWriter(); restricted.append_pages_from_reader(PdfReader(io.BytesIO(pdf_bytes(1))))
        restricted.encrypt(user_password='', owner_password='fixture-owner', permissions_flag=UserAccessPermissions.PRINT)
        for writer in (blank, restricted):
            stream = io.BytesIO(); writer.write(stream); self.pdf_override = stream.getvalue()
            c = self.crawler(); c.delivery_mode = 'backfill'
            self.assertEqual(c.crawl(), 0)
            self.assertEqual(c.delivery_source_failure, 'item_failed')
            self.assertFalse(c.delivery_exhausted)
            self.assertEqual(c.delivery_pending_cursor['offset'], 0)

    def test_cancel_during_download_is_propagated(self):
        c = self.crawler(); c.delivery_should_cancel = lambda: self.pdf_get.called
        with self.assertRaises(CrawlCancelled): c.crawl()
        self.assertEqual(self.count(), 0)

    def test_recovered_pdf_cursor_resumes_and_preserves_existing_rows(self):
        c = self.crawler(); c.delivery_mode = 'backfill'
        self.assertEqual(c.crawl(limit=3), 3)
        before = self.identities(); cursor = c.delivery_pending_cursor
        c = self.crawler(); c.delivery_mode = 'backfill'; c.delivery_cursor = cursor
        self.assertEqual(c.crawl(), 5)
        self.assertTrue(c.delivery_exhausted)
        self.assertTrue(all(self.identities()[uid] == seq for uid, seq in before.items()))
        self.assertEqual(self.pdf_get.call_count, 1)
        cursor = c.delivery_pending_cursor
        c = self.crawler(); c.delivery_mode = 'backfill'; c.delivery_cursor = cursor
        self.assertEqual(c.crawl(), 0)
        self.assertTrue(c.delivery_exhausted)
        self.pdf_get.assert_not_called()


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires disposable owned PostgreSQL')
class AcprPdfEnrichmentPostgres(AcprPdfEnrichment):
    BACKEND = 'postgres'
