"""Keep readable PDF downloads distinct from permission to extract text."""
import io
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.constants import UserAccessPermissions

from delivery.files import worker
from tests.test_file_pipeline import make_pdf


class PDFTextPermissions(unittest.TestCase):
    def stage(self, *, copy_allowed, user_password=''):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = PdfWriter()
            writer.append_pages_from_reader(PdfReader(io.BytesIO(make_pdf())))
            permissions = UserAccessPermissions.PRINT
            if copy_allowed:
                permissions |= UserAccessPermissions.EXTRACT
            writer.encrypt(user_password=user_password, owner_password='fixture-owner',
                           permissions_flag=permissions)
            with (root/'source.pdf').open('wb') as handle:
                writer.write(handle)
            result = worker.run_stage('text', root, timeout=20)
            text = (root/'output.txt').read_text() if (root/'output.txt').exists() else None
            return result, text

    def test_readable_copy_restricted_pdf_has_explicit_non_text_result(self):
        result, text = self.stage(copy_allowed=False)
        self.assertEqual(result, {'success': False, 'error': 'pdf_copy_restricted'})
        self.assertIsNone(text)

    def test_readable_encrypted_pdf_with_copy_permission_is_supported(self):
        result, text = self.stage(copy_allowed=True)
        self.assertTrue(result['success'], result)
        self.assertIn('Delivery PDF pipeline test', text)

    def test_password_required_is_distinct_from_a_broken_pdf(self):
        result, text = self.stage(copy_allowed=True, user_password='fixture-secret')
        self.assertEqual(result, {'success': False, 'error': 'pdf_password_required'})
        self.assertIsNone(text)
