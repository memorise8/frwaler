"""Download contract: bytes, database backend, and failed refresh preservation."""
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import requests

from crawler import blob_storage, db_libertree
from crawler.pdf_downloader import download_pdf_for

PDF = b'%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n'


def session_for(content, *, status=200, headers=None):
    session = mock.Mock()
    session.headers = {}
    response = mock.MagicMock()
    response.headers = headers or {}
    response.status_code = status
    response.__enter__.return_value = response
    response.iter_content.return_value = [content[:5], b'', content[5:]]
    if status >= 400:
        response.raise_for_status.side_effect = requests.HTTPError('blocked')
    session.get.return_value = response
    return session


class PdfDownloadTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        db_libertree.init_db(self.conn)
        db_libertree.upsert_site(self.conn, 'pdf-test', 'PDF Test', 'https://example.test')
        self.seq = db_libertree.insert_document(self.conn, {
            'site_id': 'pdf-test', 'title': 'Test', 'meta_url': 'https://example.test/1',
        })

    def download(self, content, **kwargs):
        session = kwargs.pop('session', session_for(content))
        return download_pdf_for(self.conn, self.seq, 'https://example.test/a.pdf',
                                blob_root=self.temp.name, session=session, **kwargs)

    def test_pdf_bytes_saved_with_hash_even_with_generic_mime_and_wrong_env(self):
        with mock.patch.dict(os.environ, {'LIBERTREE_DB_BACKEND': 'postgres'}):
            result = self.download(PDF, session=session_for(PDF, headers={'Content-Type': 'application/octet-stream'}))
        self.assertTrue(result['success'])
        self.assertEqual(Path(result['path']).read_bytes(), PDF)
        row = db_libertree.get_document(self.conn, self.seq)
        self.assertEqual(row['pdf_sha256'], hashlib.sha256(PDF).hexdigest())
        self.assertEqual(row['pdf_downloaded'], 1)

    def test_html_rejected_even_if_server_claims_pdf(self):
        content = b'<html><body>Access denied %PDF-1.7</body></html>'
        result = self.download(content, session=session_for(content, headers={'Content-Type': 'application/pdf'}))
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'invalid PDF header')
        self.assertEqual(db_libertree.get_document(self.conn, self.seq)['pdf_downloaded'], 0)
        self.assertFalse(blob_storage.has_blob(self.seq, root=self.temp.name))

    def test_empty_http_error_and_size_limits_do_not_create_files(self):
        for session, options in [
            (session_for(b''), {}), (session_for(PDF, status=403), {}),
            (session_for(PDF, headers={'Content-Length': '999'}), {'max_size': 20}),
            (session_for(PDF), {'max_size': 20}),
        ]:
            with self.subTest(options=options, status=session.get.return_value.status_code):
                result = self.download(b'', session=session, **options)
                self.assertFalse(result['success'])
                self.assertFalse(blob_storage.has_blob(self.seq, root=self.temp.name))

    def test_failed_refresh_preserves_existing_good_file_and_metadata(self):
        result = self.download(PDF)
        before = dict(db_libertree.get_document(self.conn, self.seq))
        self.assertFalse(self.download(b'<html>Login</html>')['success'])
        self.assertEqual(Path(result['path']).read_bytes(), PDF)
        self.assertEqual(dict(db_libertree.get_document(self.conn, self.seq)), before)

    def test_failed_file_replace_preserves_previous_pdf_and_cleans_temp(self):
        result = self.download(PDF)
        before = dict(db_libertree.get_document(self.conn, self.seq))
        with mock.patch('crawler.blob_storage.os.replace', side_effect=OSError('disk error')):
            self.assertFalse(self.download(PDF + b'new')['success'])
        self.assertEqual(Path(result['path']).read_bytes(), PDF)
        self.assertEqual(dict(db_libertree.get_document(self.conn, self.seq)), before)
        self.assertEqual(list(Path(result['path']).parent.iterdir()), [Path(result['path'])])

    def test_owned_session_closed_on_request_failure(self):
        session = session_for(b'')
        session.get.side_effect = requests.Timeout('timed out')
        with mock.patch('crawler.pdf_downloader.requests.Session', return_value=session):
            result = download_pdf_for(self.conn, self.seq, 'https://example.test/a.pdf', blob_root=self.temp.name)
        self.assertFalse(result['success'])
        session.close.assert_called_once()

    def test_caller_session_remains_open(self):
        session = session_for(PDF)
        self.assertTrue(self.download(PDF, session=session)['success'])
        session.close.assert_not_called()


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'set TEST_PG_DSN to run')
class PdfPostgresTest(unittest.TestCase):
    def test_real_postgres_download_updates_flags_and_rejects_html(self):
        from crawler import db_pg
        conn = db_pg.open_db(os.environ['TEST_PG_DSN'])
        self.addCleanup(conn.close)
        db_pg.init_db(conn)
        db_pg.upsert_site(conn, 'pdf-pg-test', 'PDF PG Test', 'https://example.test')
        seq = db_pg.insert_document(conn, {'site_id': 'pdf-pg-test', 'title': 'Test',
                                          'meta_url': 'https://example.test/pdf-pg'})
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, {'LIBERTREE_DB_BACKEND': 'sqlite'}):
            result = download_pdf_for(conn, seq, 'https://example.test/file', blob_root=root, session=session_for(PDF))
            self.assertTrue(result['success'])
            self.assertEqual(db_pg.get_document(conn, seq)['pdf_downloaded'], 1)
            rejected = download_pdf_for(conn, seq, 'https://example.test/file', blob_root=root, session=session_for(b'<html/>'))
            self.assertFalse(rejected['success'])
            self.assertEqual(db_pg.get_document(conn, seq)['pdf_sha256'], result['sha256'])
            self.assertEqual(Path(result['path']).read_bytes(), PDF)
