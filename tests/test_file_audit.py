import contextlib
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from crawler import db_libertree
from delivery.health.file_audit import digest, run, run_stage


def text_pdf():
    stream = b'BT /F1 12 Tf 20 100 Td (Source audit scientific report) Tj ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream']
    data = b'%PDF-1.4\n'; offsets = []
    for n, obj in enumerate(objects, 1):
        offsets.append(len(data)); data += str(n).encode()+b' 0 obj\n'+obj+b'\nendobj\n'
    start = len(data)
    data += b'xref\n0 6\n0000000000 65535 f \n'
    data += b''.join(f'{offset:010} 00000 n \n'.encode() for offset in offsets)
    return data+b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n'+str(start).encode()+b'\n%%EOF\n'


class FileAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.db = self.root/'source.sqlite'
        with sqlite3.connect(self.db) as conn:
            db_libertree.init_db(conn); db_libertree.upsert_site(conn, 'file-test', 'File Test', 'https://example.test')
            for n in range(3):
                db_libertree.insert_document(conn, {'site_id': 'file-test', 'title': f'Title {n}',
                    'post_number': str(n), 'meta_url': f'https://example.test/{n}', 'pdf_url': f'https://example.test/{n}.pdf'})
        self.source_hash = digest(self.db)
        self.review = self.root/'review.json'; self.review.write_text(json.dumps({'database_sha256': self.source_hash, 'actual_stored_rows': 3}))
        self.manifest = self.root/'manifest.json'; self.manifest.write_text(json.dumps({'source_count': 4, 'records': [
            {'post_number': str(n), 'code_save_eligible': True, 'native_primary_file_url': f'https://example.test/{n}.pdf'} for n in range(3)]}))
        self.gate = Mock(); self.gate.enter.side_effect = lambda url: contextlib.nullcontext()
        self.enterContext(patch('delivery.health.file_audit.RequestGate', return_value=self.gate))
        self.enterContext(patch('delivery.health.file_audit.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('8.8.8.8', 443))]))
        self.status = 200; self.body = text_pdf(); self.location = None; self.requests = []; self.failures_left = 0
        def send(adapter, request, **kwargs):
            self.requests.append(request.url)
            if self.failures_left:
                self.failures_left -= 1
                raise requests.ConnectionError('remote closed connection')
            response = requests.Response(); response.status_code = self.status
            response.request = request; response.url = request.url; response.raw = io.BytesIO(self.body)
            response.headers['Retry-After'] = '120'
            if self.location: response.headers['Location'] = self.location
            return response
        self.enterContext(patch.object(requests.adapters.HTTPAdapter, 'send', send))

    def audit(self, **kwargs):
        result = run(self.root/'out', self.db, self.review, self.manifest, self.root/'gate', **kwargs)
        self.assertEqual(digest(self.db), self.source_hash)
        return result

    def test_native_pdf_and_real_parser_checkpoint_resume_and_no_refetch(self):
        first = self.audit(stop_after=1)
        self.assertEqual(first['state'], 'checkpointed')
        self.assertEqual(len(self.requests), 1)
        second = self.audit(resume=True)
        self.assertEqual(second['state'], 'manifest_files_verified')
        self.assertEqual(second['runs'][-1]['reused_verified'], 1)
        third = self.audit(resume=True)
        self.assertEqual(third['runs'][-1]['reused_verified'], 3)
        self.assertEqual(third['runs'][-1]['newly_processed'], 0)
        self.assertEqual(len(self.requests), 3)
        self.assertFalse(third['full_collection_verified'])
        with sqlite3.connect(self.root/'out/files.sqlite') as conn:
            self.assertEqual(conn.execute('SELECT sum(pdf_downloaded),sum(text_extracted) FROM documents').fetchone(), (3, 3))
        for f in third['files'].values():
            self.assertEqual(f['pdf_sha256'], hashlib.sha256(self.body).hexdigest())
            self.assertGreater(f['extracted_characters'], 20)

    def test_html_response_stops_without_pdf_flag_or_other_document_requests(self):
        self.body = b'<html>Access challenge</html>'
        result = self.audit()
        self.assertEqual(result['reason'], 'native_file_download_failed')
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result['files']['0']['error'], 'invalid PDF header')
        with sqlite3.connect(self.root/'out/files.sqlite') as conn:
            self.assertEqual(conn.execute('SELECT sum(pdf_downloaded) FROM documents').fetchone()[0], 0)

    def test_denial_is_terminal_without_retry(self):
        self.status = 403
        result = self.audit()
        self.assertEqual(result['reason'], 'source_access_denied')
        self.assertEqual(len(self.requests), 1)
        with self.assertRaises(ValueError): self.audit(resume=True)

    def test_rate_limit_is_recorded_and_deferred(self):
        self.status = 429
        result = self.audit()
        self.assertEqual(result['state'], 'host_cooldown_deferred')
        self.gate.defer.assert_called_once()
        self.assertEqual(len(self.requests), 1)

    def test_unreviewed_redirect_is_not_requested(self):
        self.status = 302; self.location = 'https://outside.test/file.pdf'
        result = self.audit()
        self.assertEqual(result['reason'], 'redirect_or_request_outside_reviewed_hosts')
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result['requests'][0]['status'], 302)

    def test_cached_file_corruption_is_detected_before_resume(self):
        self.audit(stop_after=1)
        pdf = next((self.root/'out/blobs').rglob('*.pdf')); pdf.write_bytes(b'corrupted')
        result = self.audit(resume=True)
        self.assertEqual(result['reason'], 'cached_file_integrity_mismatch')
        self.assertEqual(len(self.requests), 1)

    def test_single_parser_failure_keeps_other_document_verification_running(self):
        calls = []
        def extract(*args, **kwargs):
            calls.append(1)
            return {'success': False, 'error': 'parse_failed'} if len(calls) == 1 else run_stage(*args, **kwargs)
        with patch('delivery.health.file_audit.run_stage', side_effect=extract):
            result = self.audit()
        self.assertEqual(result['state'], 'manifest_files_incomplete')
        self.assertEqual(len(self.requests), 3)
        self.assertEqual([v['state'] for v in result['files'].values()], ['text_failed', 'verified', 'verified'])

    def test_transient_transport_error_has_one_bounded_retry(self):
        self.failures_left = 1
        with patch('delivery.health.file_audit.time.sleep'):
            result = self.audit()
        self.assertEqual(result['state'], 'manifest_files_verified')
        self.assertEqual(len(self.requests), 4)
        self.assertEqual([v['success'] for v in result['files']['0']['download_attempts']], [False, True])

    def test_repeated_transport_failure_keeps_other_files_running(self):
        self.failures_left = 2
        with patch('delivery.health.file_audit.time.sleep'):
            result = self.audit()
        self.assertEqual(result['state'], 'manifest_files_incomplete')
        self.assertEqual(len(self.requests), 4)
        self.assertEqual([v['state'] for v in result['files'].values()], ['download_failed', 'verified', 'verified'])

    def test_reviewed_legacy_transport_failure_can_resume_preserved_files(self):
        self.audit(stop_after=1)
        file = self.root/'out/files.json'; report = json.loads(file.read_text())
        report.update(state='incomplete', reason='native_file_download_failed')
        report['files']['1'] = {'seq_id': 2, 'state': 'download_failed', 'error': 'request error: disconnected'}
        file.write_text(json.dumps(report))
        with self.assertRaises(ValueError): self.audit(resume=True)
        result = self.audit(resume=True, retry_transport_failure=True)
        self.assertEqual(result['state'], 'manifest_files_verified')
        self.assertEqual(result['runs'][-1]['reused_verified'], 1)
        self.assertEqual(len(result['files']['1']['download_attempts']), 2)


if __name__ == '__main__':
    unittest.main()
