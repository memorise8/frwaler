"""Source-bound PDF body recovery must not hide access/text/identity failures."""
import hashlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from crawler.base_crawler import CrawlCancelled
from crawler.kei_details import parse_detail
from crawler.kei_pdf import source_pdf_excerpt, validate_source_pdf
from tests.test_kei_details import fixture


class KeiPdfBody(unittest.TestCase):
    def setup_source(self, scope='researchreport', filename='report.pdf'):
        raw, item = fixture(scope, body='', filename=filename)
        item['source_key'] = scope + ':123'
        self.item, self.detail = item, parse_detail(raw, item)
        self.payload = b'%PDF-1.4\nRetained test PDF bytes\n'
        r = requests.Response(); r.status_code = 200; r.raw = io.BytesIO(self.payload)
        r.headers['Content-Length'] = str(len(self.payload))
        self.response = r
        self.crawler = SimpleNamespace(_MIN_ABSTRACT=50, _check_cancelled=Mock(),
                                       _session=SimpleNamespace(get=Mock(return_value=r)))
        return self.crawler

    def stage(self, stage, root, timeout):
        if stage == 'pdf_body_coverage':
            return dict(success=True, coverage={'state':'no_dominant_raster_signal','fidelity_verified':False})
        self.assertEqual(stage, 'text'); self.assertEqual(timeout, 120)
        self.assertEqual((root/'source.pdf').read_bytes(), self.payload)
        (root/'output.txt').write_text('Source report text with actual research findings. ' * 100)
        return dict(success=True, chars=len((root/'output.txt').read_text()),
                    quality={'state':'no_obvious_defects','fidelity_verified':False})

    def test_source_bytes_excerpt_and_provenance_are_bound_without_file_flags(self):
        c = self.setup_source()
        with patch('delivery.files.worker.run_stage', side_effect=self.stage):
            excerpt, meta = source_pdf_excerpt(c, self.item, self.detail)
        self.assertEqual(len(excerpt), 3000)
        self.assertEqual(meta['abstract_pdf_sha256'], hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(meta['abstract_source'], 'publisher_pdf_text_excerpt')
        self.assertFalse(meta['full_text_fidelity_verified'])
        self.assertFalse(meta['abstract_pdf_body_coverage']['fidelity_verified'])
        self.assertNotIn('pdf_downloaded', meta)
        self.assertNotIn('text_extracted', meta)
        self.assertFalse(c._session.get.call_args.kwargs['allow_redirects'])
        self.assertEqual(c._session.get.call_args.kwargs['headers']['Referer'], self.item['url'])

    def test_different_document_or_attachment_is_rejected_before_network(self):
        c = self.setup_source()
        for field,value in [('source_key','researchreport:999'),('source_url','https://outside.invalid/')]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                source_pdf_excerpt(c,self.item,dict(self.detail,**{field:value}))
        c._session.get.assert_not_called()

    def test_catalogue_download_destination_and_board_source_id_are_strict(self):
        for scope in ('researchreport','board-0028'):
            self.setup_source(scope)
            self.assertEqual(validate_source_pdf(self.item,self.detail),self.detail['attachments'][0]['url'])
            for change in [lambda u:u.replace('https://','http://'),lambda u:u.replace('library.kei.re.kr','outside.invalid').replace('www.kei.re.kr','outside.invalid'),lambda u:u+'#fragment']:
                d=dict(self.detail);f=dict(d['attachments'][0],url=change(d['attachments'][0]['url']));d.update(primary_PDF_candidate=f,attachments=[f])
                with self.assertRaises(ValueError):validate_source_pdf(self.item,d)
        d=dict(self.detail);f=dict(d['attachments'][0],url=d['attachments'][0]['url'].replace('list_no=123','list_no=999'));d.update(primary_PDF_candidate=f,attachments=[f])
        with self.assertRaises(ValueError):validate_source_pdf(self.item,d)

    def test_hwp_cannot_be_extracted_as_pdf(self):
        c=self.setup_source('board-0009','report.hwp')
        with self.assertRaises(ValueError):source_pdf_excerpt(c,self.item,self.detail)
        c._session.get.assert_not_called()

    def test_access_and_redirects_hold_without_extraction(self):
        for status in (401,403,429,503,302,404):
            c=self.setup_source();self.response.status_code=status
            with patch('delivery.files.worker.run_stage') as stage, self.assertRaises((PermissionError,ConnectionError)):
                source_pdf_excerpt(c,self.item,self.detail)
            stage.assert_not_called()

    def test_html_truncated_and_oversize_files_do_not_reach_extraction(self):
        for kind in ('html','truncated','oversize'):
            c=self.setup_source()
            if kind=='html':self.response.raw=io.BytesIO(b'<html>Denied</html>');self.response.headers.pop('Content-Length')
            elif kind=='truncated':self.response.headers['Content-Length']='10000'
            else:self.response.headers['Content-Length']=str(201*1024**2)
            with patch('delivery.files.worker.run_stage') as stage,self.assertRaises(ValueError):source_pdf_excerpt(c,self.item,self.detail)
            stage.assert_not_called()

    def test_no_text_and_severe_quality_remain_explicit_holds(self):
        for result in [dict(success=False,error='no_text_layer'),dict(success=False,error='copy_restricted'),dict(success=True,quality={'state':'review_required'})]:
            c=self.setup_source()
            with patch('delivery.files.worker.run_stage',return_value=result),self.assertRaises(ValueError):source_pdf_excerpt(c,self.item,self.detail)

    def test_short_or_corrupt_excerpt_cannot_pass_on_full_text_success(self):
        for value in ('short','\ufffd'*3000):
            c=self.setup_source()
            def stage(stage,root,timeout):
                (root/'output.txt').write_text(value)
                return dict(success=True,chars=len(value),quality={'state':'no_obvious_defects'})
            with patch('delivery.files.worker.run_stage',side_effect=stage),self.assertRaises(ValueError):source_pdf_excerpt(c,self.item,self.detail)

    def test_mild_diagnostics_are_preserved_without_claiming_fidelity(self):
        c=self.setup_source()
        def stage(stage,root,timeout):
            if stage=='pdf_body_coverage':return self.stage(stage,root,timeout)
            value='Report text. '*300+'\uf09f';(root/'output.txt').write_text(value)
            return dict(success=True,chars=len(value),quality={'state':'review','signals':['private_use_characters']})
        with patch('delivery.files.worker.run_stage',side_effect=stage):_,meta=source_pdf_excerpt(c,self.item,self.detail)
        self.assertEqual(meta['abstract_pdf_text_quality']['state'],'review')
        self.assertFalse(meta['full_text_fidelity_verified'])

    def test_long_clean_text_does_not_override_dominant_raster_body_or_failed_inspection(self):
        for error in ('pdf_body_coverage_review_required','stage_timeout','pdf_body_coverage_unavailable'):
            c=self.setup_source()
            def stage(name,root,timeout):
                if name=='pdf_body_coverage':return dict(success=False,error=error)
                return self.stage(name,root,timeout)
            with patch('delivery.files.worker.run_stage',side_effect=stage),self.assertRaisesRegex(ValueError,error):
                source_pdf_excerpt(c,self.item,self.detail)

    def test_initial_cancellation_happens_before_download(self):
        c=self.setup_source();c._check_cancelled.side_effect=CrawlCancelled()
        with self.assertRaises(CrawlCancelled):source_pdf_excerpt(c,self.item,self.detail)
        c._session.get.assert_not_called()


if __name__=='__main__':unittest.main()
