"""Exercise corrupt text, permission-safe fallback, cached files and durable diagnostics."""
import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pypdf import PdfReader, PdfWriter
from pypdf.constants import UserAccessPermissions
from delivery.files import jobs, stage, worker
from delivery.files.text_quality import inspect_text
from tests import test_file_pipeline as fixture

BAD='(cid:42)'*100
CONTROL='\x02\x07'*400
GOOD='Readable source words, preserved exactly. '*50

class TextQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        (self.root/'source.pdf').write_bytes(fixture.make_pdf())

    def test_mild_signals_are_reviewable_without_being_declared_unreadable(self):
        q=inspect_text(GOOD+' (cid:42) \ufffd \ue000')
        self.assertEqual(q['state'],'review');self.assertFalse(q['fidelity_verified']);self.assertEqual(len(q['signals']),3)
        self.assertEqual(inspect_text(GOOD)['state'],'no_obvious_defects')

    def test_cid_control_replacement_and_private_use_heavy_text_are_held(self):
        for text in (BAD,CONTROL,'\ufffd'*100,'\ue000'*100):
            self.assertEqual(inspect_text(text)['state'],'review_required')
        self.assertEqual(inspect_text('A'*451+'\ufffd'*49)['state'],'review')
        self.assertEqual(inspect_text('A'*450+'\ufffd'*50)['state'],'review_required')
        self.assertEqual(inspect_text('A'*10000+'\x02'*100)['state'],'review_required')
        self.assertEqual(inspect_text('A'*100+'\t\n\r\f'*100)['state'],'no_obvious_defects')

    def test_primary_garbled_output_does_not_publish_a_text_file(self):
        (self.root/'output.txt').write_text('stale partial output')
        with mock.patch('crawler.converter.extract_text_pdf',return_value=BAD):result=stage.execute('text',self.root)
        self.assertEqual(result['error'],'text_quality_review_required');self.assertFalse((self.root/'output.txt').exists())

    def test_real_fallback_preserves_public_reader_text(self):
        result=worker.run_stage('text_fallback',self.root,timeout=20)
        self.assertTrue(result['success'],result);self.assertEqual(result['quality']['engine'],'pdftotext')
        self.assertIn('Delivery PDF pipeline test',(self.root/'output.txt').read_text())

    def test_timeout_recovery_uses_a_new_bounded_isolated_stage(self):
        original=worker._run_stage_once;calls=[]
        def first_timeout(name,*args,**kwargs):
            calls.append((name,kwargs['timeout']))
            if name=='text':
                (self.root/'output.txt').write_text('stale timeout output')
                return {'success':False,'error':'stage_timeout'}
            return original(name,*args,**kwargs)
        with mock.patch.object(worker,'_run_stage_once',side_effect=first_timeout):result=worker.run_stage('text',self.root,timeout=120)
        self.assertTrue(result['success'],result);self.assertEqual(calls,[('text',120),('text_fallback',60)])
        self.assertEqual(result['quality']['recovery_from'],'stage_timeout');self.assertNotIn('stale',(self.root/'output.txt').read_text())

    def test_zero_cid_but_control_code_fallback_is_not_a_recovery(self):
        original=worker._run_stage_once
        def run(name,*args,**kwargs):
            if name=='text':return {'success':False,'error':'text_quality_review_required','quality':inspect_text(BAD)}
            return {'success':False,'error':'text_quality_review_required','quality':inspect_text(CONTROL)}
        with mock.patch.object(worker,'_run_stage_once',side_effect=run):result=worker.run_stage('text',self.root,timeout=20)
        self.assertFalse(result['success']);self.assertEqual(result['quality']['fallback']['counts']['unmapped_glyph_markers'],0)
        self.assertGreater(result['quality']['fallback']['counts']['control_characters'],0)

    def encrypted(self,copy_allowed=False,password=''):
        writer=PdfWriter();writer.append_pages_from_reader(PdfReader(io.BytesIO(fixture.make_pdf())))
        permissions=UserAccessPermissions.PRINT
        if copy_allowed:permissions|=UserAccessPermissions.EXTRACT
        writer.encrypt(user_password=password,owner_password='fixture-owner',permissions_flag=permissions)
        with (self.root/'source.pdf').open('wb') as h:writer.write(h)

    def test_direct_fallback_cannot_bypass_copy_restriction(self):
        self.encrypted()
        with mock.patch.object(stage.subprocess,'run',side_effect=AssertionError('must not invoke extractor')):result=stage.execute('text_fallback',self.root)
        self.assertEqual(result,{'success':False,'error':'pdf_copy_restricted'})

    def test_direct_fallback_cannot_bypass_reader_password(self):
        self.encrypted(copy_allowed=True,password='fixture-user')
        with mock.patch.object(stage.subprocess,'run',side_effect=AssertionError('must not invoke extractor')):result=stage.execute('text_fallback',self.root)
        self.assertEqual(result,{'success':False,'error':'pdf_password_required'})

    def test_permitted_encrypted_fallback_is_supported_without_owner_password(self):
        self.encrypted(copy_allowed=True);result=worker.run_stage('text_fallback',self.root,timeout=20)
        self.assertTrue(result['success'],result);self.assertIn('Delivery PDF pipeline test',(self.root/'output.txt').read_text())

    def test_primary_access_failure_never_attempts_another_extractor(self):
        with mock.patch.object(worker,'_run_stage_once',return_value={'success':False,'error':'pdf_copy_restricted'}) as run:
            self.assertEqual(worker.run_stage('text',self.root)['error'],'pdf_copy_restricted')
        self.assertEqual(run.call_count,1)

    def test_fallback_output_is_checked_even_when_executable_succeeds(self):
        def corrupt(*args,**kwargs):
            (self.root/'fallback.txt').write_text(CONTROL)
            return mock.Mock(returncode=0)
        with mock.patch.object(stage.subprocess,'run',side_effect=corrupt):result=stage.execute('text_fallback',self.root)
        self.assertEqual(result['error'],'text_quality_review_required');self.assertFalse((self.root/'output.txt').exists())

    def test_missing_fallback_does_not_hide_original_failure(self):
        with mock.patch.object(worker,'_run_stage_once',side_effect=[{'success':False,'error':'stage_timeout'},{'success':False,'error':'text_extractor_unavailable'}]):result=worker.run_stage('text',self.root)
        self.assertEqual(result['error'],'stage_timeout');self.assertEqual(result['fallback_error'],'text_extractor_unavailable')


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires disposable PostgreSQL')
class FileQualityPostgresTests(unittest.TestCase):
    setUp=fixture.FilePipelinePostgresTest.setUp
    queue=fixture.FilePipelinePostgresTest.queue
    execute=fixture.FilePipelinePostgresTest.execute

    def write_cached(self,seq,text):
        root=Path(self.temp.name);pdf=worker.blob_storage.get_blob_path(seq,'pdf',root);txt=worker.blob_storage.get_blob_path(seq,'txt',root);pdf.parent.mkdir(parents=True,exist_ok=True);pdf.write_bytes(fixture.make_pdf());txt.write_text(text)
        self.db.update_document_pdf(self.conn,seq,downloaded=True,size_bytes=pdf.stat().st_size,sha256=hashlib.sha256(pdf.read_bytes()).hexdigest());self.db.update_document_text(self.conn,seq,extracted=True)
        return pdf,txt

    def test_cached_mild_warning_is_preserved_and_exposed_in_summary(self):
        jid,seq=self.queue();pdf,txt=self.write_cached(seq,GOOD+'(cid:1)');before=txt.read_bytes()
        worker.process(self.conn,jobs.claim(self.conn),root=self.temp.name,executor=mock.Mock(side_effect=AssertionError('reuse text')))
        r=jobs.summary(self.conn,jid);self.assertEqual((r['done'],r['text_quality_review']),(1,1));self.assertEqual(r['items'][0]['text_quality']['state'],'review');self.assertEqual(txt.read_bytes(),before)

    def test_corrupt_cached_text_is_recovered_without_redownloading_PDF(self):
        jid,seq=self.queue();pdf,txt=self.write_cached(seq,BAD);before_pdf=pdf.read_bytes();doc=self.db.get_document(self.conn,seq)
        def execute(name,*args,**kwargs):
            self.assertEqual(name,'text');return worker.run_stage(name,*args,timeout=20)
        worker.process(self.conn,jobs.claim(self.conn),root=self.temp.name,executor=execute)
        r=jobs.summary(self.conn,jid);self.assertEqual((r['done'],r['text_quality_review']),(1,0));self.assertEqual(pdf.read_bytes(),before_pdf);self.assertIn('Delivery PDF pipeline test',txt.read_text());after=self.db.get_document(self.conn,seq)
        self.assertTrue(after['text_extracted']);self.assertEqual({k:v for k,v in doc.items() if k!='text_extracted'},{k:v for k,v in after.items() if k!='text_extracted'})

    def test_unrecovered_corrupt_cache_is_preserved_but_not_advertised_as_valid(self):
        from delivery.be.blob_delivery import resolve_blob
        jid,seq=self.queue();pdf,txt=self.write_cached(seq,BAD);before=txt.read_bytes()
        def failed(*args,**kwargs):return {'success':False,'error':'text_quality_review_required','quality':inspect_text(BAD)}
        worker.process(self.conn,jobs.claim(self.conn),root=self.temp.name,executor=failed)
        r=jobs.summary(self.conn,jid);self.assertEqual((r['failed'],r['pdf_done'],r['text_done'],r['text_quality_review']),(1,1,0,1));self.assertEqual(txt.read_bytes(),before);self.assertEqual(self.db.get_document(self.conn,seq)['text_extracted'],0)
        with self.assertRaises(FileNotFoundError):resolve_blob(self.conn,seq,'text')

    def test_custom_executor_cannot_publish_garbled_text_as_success(self):
        jid,seq=self.queue()
        def bad(name,directory,*args,**kwargs):
            if name=='pdf':return self.execute(name,directory,*args,**kwargs)
            (Path(directory)/'output.txt').write_text(BAD);return {'success':True,'chars':len(BAD),'quality':inspect_text(GOOD)}
        worker.process(self.conn,jobs.claim(self.conn),root=self.temp.name,executor=bad)
        r=jobs.summary(self.conn,jid);self.assertEqual(r['items'][0]['error'],'text_quality_review_required');self.assertEqual(r['text_done'],0);self.assertFalse(worker.blob_storage.get_blob_path(seq,'txt',self.temp.name).exists())

    def test_cancelled_repair_cannot_publish_or_finish_the_cancelled_job(self):
        jid,seq=self.queue();pdf,txt=self.write_cached(seq,BAD);before=txt.read_bytes()
        def cancelled(name,directory,**kwargs):
            result=worker.run_stage(name,directory,timeout=20);jobs.cancel(self.conn,jid);return result
        worker.process(self.conn,jobs.claim(self.conn),root=self.temp.name,executor=cancelled)
        r=jobs.summary(self.conn,jid);self.assertEqual(r['cancelled'],1);self.assertEqual(txt.read_bytes(),before);self.assertEqual(self.db.get_document(self.conn,seq)['text_extracted'],0)

    def test_additive_quality_migration_preserves_existing_jobs_and_documents(self):
        from delivery.db.schema import init_delivery_schema
        jid,seq=self.queue();before=dict(self.conn.execute('SELECT * FROM documents WHERE seq_id=%s',(seq,)).fetchone());old=dict(self.conn.execute('SELECT * FROM document_file_jobs WHERE crawl_job_id=%s',(jid,)).fetchone());self.conn.execute('ALTER TABLE document_file_jobs DROP COLUMN text_quality');self.conn.commit();init_delivery_schema(self.conn);init_delivery_schema(self.conn)
        self.assertEqual(dict(self.conn.execute('SELECT * FROM documents WHERE seq_id=%s',(seq,)).fetchone()),before);self.assertEqual(dict(self.conn.execute('SELECT * FROM document_file_jobs WHERE crawl_job_id=%s',(jid,)).fetchone()),old)

if __name__=='__main__':unittest.main()
