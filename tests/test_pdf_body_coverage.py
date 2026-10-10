"""Real PDF layout probes distinguish image-dominated bodies from text reports."""
import io
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfWriter
from pypdf.constants import UserAccessPermissions
from pypdf.generic import DictionaryObject, NameObject, NumberObject, DecodedStreamObject

from delivery.files.worker import run_stage


def make_document(image_pages, text_pages, *, fragments=False, copy_allowed=True, password=''):
    writer = PdfWriter()
    font = DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),
                             NameObject('/BaseFont'):NameObject('/Helvetica')})
    img = DecodedStreamObject()
    img.set_data(b'\x80\x80\x80')
    img.update({NameObject('/Type'):NameObject('/XObject'),NameObject('/Subtype'):NameObject('/Image'),
                NameObject('/Width'):NumberObject(1),NameObject('/Height'):NumberObject(1),
                NameObject('/ColorSpace'):NameObject('/DeviceRGB'),NameObject('/BitsPerComponent'):NumberObject(8)})
    for i in range(image_pages+text_pages):
        page = writer.add_blank_page(width=300,height=400)
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)}),
                                                           NameObject('/XObject'):DictionaryObject({NameObject('/Im1'):writer._add_object(img)})})
        if i < image_pages:
            text = b'Header and page number'
            graphics = (b' '.join(f'q 200 0 0 10 30 {30+j*20} cm /Im1 Do Q'.encode() for j in range(12))
                        if fragments else b'q 240 0 0 320 30 40 cm /Im1 Do Q')
        else:
            text = b'The report describes actual findings and supports a complete research discussion. '*5
            graphics = b''
        stream=DecodedStreamObject();stream.set_data(graphics+b' BT /F1 10 Tf 10 380 Td ('+text+b') Tj ET')
        page[NameObject('/Contents')]=writer._add_object(stream)
    if not copy_allowed or password:
        permissions=UserAccessPermissions.PRINT
        if copy_allowed:permissions |= UserAccessPermissions.EXTRACT
        writer.encrypt(user_password=password,owner_password='fixture-owner',permissions_flag=permissions)
    out=io.BytesIO();writer.write(out);return out.getvalue()


class PDFBodyCoverage(unittest.TestCase):
    def probe(self, payload):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'source.pdf').write_bytes(payload)
            (root/'output.txt').write_text('Previously extracted text stays intact.')
            result=run_stage('pdf_body_coverage',root,timeout=20)
            self.assertEqual((root/'output.txt').read_text(),'Previously extracted text stays intact.')
            return result

    def test_clean_headers_do_not_prove_full_page_or_fragmented_raster_body(self):
        for fragments in (False,True):
            r=self.probe(make_document(4,1,fragments=fragments))
            self.assertFalse(r['success'])
            self.assertEqual(r['error'],'pdf_body_coverage_review_required')
            self.assertEqual(r['coverage']['sparse_image_pages'],[1,2,3,4])
            self.assertFalse(r['coverage']['OCR_enabled'])

    def test_cover_and_minority_figure_pages_do_not_reject_readable_report(self):
        for image_pages,text_pages in ((1,4),(3,4),(3,3),(0,4)):
            r=self.probe(make_document(image_pages,text_pages))
            self.assertTrue(r['success'],r)
            self.assertFalse(r['coverage']['fidelity_verified'])

    def test_permissions_and_passwords_are_preserved(self):
        for payload,code in ((make_document(4,1,copy_allowed=False),'pdf_copy_restricted'),
                             (make_document(4,1,password='secret'),'pdf_password_required')):
            r=self.probe(payload);self.assertEqual(r,{'success':False,'error':code})

    def test_invalid_pdf_is_explicit_inspection_failure(self):
        self.assertEqual(self.probe(b'broken PDF'),{'success':False,'error':'pdf_body_coverage_unavailable'})
