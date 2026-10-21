"""File pipeline behavior; PostgreSQL integration requires a disposable DSN."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from delivery.files import worker


def make_pdf():
    # A small real PDF with a text layer, no external fixture dependency.
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    stream=b'BT /F1 14 Tf 30 100 Td (Delivery PDF pipeline test) Tj ET'
    objects.append(b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream')
    pdf=b'%PDF-1.4\n'
    offsets=[0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf+=str(index).encode()+b' 0 obj\n'+obj+b'\nendobj\n'
    start=len(pdf)
    pdf+=b'xref\n0 6\n0000000000 65535 f \n'
    for offset in offsets[1:]:
        pdf+=f'{offset:010d} 00000 n \n'.encode()
    return pdf+b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n'+str(start).encode()+b'\n%%EOF\n'


class FileStageTest(unittest.TestCase):
    def test_real_pdf_text_extraction_in_child_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'source.pdf').write_bytes(make_pdf())
            result=worker.run_stage('text', root, timeout=20)
            self.assertTrue(result['success'], result)
            self.assertIn('Delivery PDF pipeline test', (root/'output.txt').read_text())

    def test_hard_timeout_kills_child(self):
        original=subprocess.Popen
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(worker.subprocess,'Popen',side_effect=lambda *a,**kw:original([sys.executable,'-c','import time; time.sleep(20)'],**kw)):
            result=worker.run_stage('text',Path(directory),timeout=0.1)
        self.assertEqual(result['error'],'stage_timeout')

    def test_existing_file_requires_header_and_matching_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf=Path(directory)/'test.pdf'
            pdf.write_bytes(make_pdf())
            self.assertTrue(worker.pdf_is_valid(pdf,hashlib.sha256(pdf.read_bytes()).hexdigest()))
            self.assertFalse(worker.pdf_is_valid(pdf,'bad-hash'))
            pdf.write_bytes(b'<html>Login %PDF-1.7</html>')
            self.assertFalse(worker.pdf_is_valid(pdf))

    def test_pdf_without_text_layer_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            marker=b'Delivery PDF pipeline test'
            (root/'source.pdf').write_bytes(make_pdf().replace(marker,b' '*len(marker)))
            result=worker.run_stage('text',root,timeout=20)
            self.assertEqual(result,{'success':False,'error':'no_text_layer'})

    def test_bad_pdf_is_not_successful_text_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'source.pdf').write_bytes(b'%PDF-1.7 broken file')
            result=worker.run_stage('text',root,timeout=20)
            self.assertFalse(result['success'])
            self.assertEqual(result['error'],'parse_failed')


class FileProcessingSafetyTest(unittest.TestCase):
    """Real files/parsers with DB calls isolated; not a substitute for PG tests."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.conn=mock.Mock()
        self.conn.execute.return_value.fetchone.return_value={'locked':True}
        self.task={'id':7,'seq_id':42,'token':'test-token','attempts':1}
        self.doc={'seq_id':42,'pdf_url':'https://fixture.invalid/file.pdf','pdf_downloaded':0,'text_extracted':0,'pdf_sha256':None}
        self.finish=self.enterContext(mock.patch('delivery.files.jobs.finish'))
        self.owns=self.enterContext(mock.patch('delivery.files.jobs.owns',return_value=True))
        self.enterContext(mock.patch('crawler.db_pg.get_document',return_value=self.doc))
        self.pdf_update=self.enterContext(mock.patch('crawler.db_pg.update_document_pdf'))
        self.text_update=self.enterContext(mock.patch('crawler.db_pg.update_document_text'))
        self.pdf=worker.blob_storage.get_blob_path(42,'pdf',self.root)
        self.txt=worker.blob_storage.get_blob_path(42,'txt',self.root)

    def executor(self,stage,directory,url=None,**kwargs):
        if stage=='pdf':
            content=make_pdf()
            (Path(directory)/'download.pdf').write_bytes(content)
            return {'success':True,'sha256':hashlib.sha256(content).hexdigest()}
        return worker.run_stage(stage,directory,timeout=20)

    def process(self,executor=None):
        worker.process(self.conn,self.task,root=self.root,executor=executor or self.executor)

    def test_pdf_and_text_are_published_and_recorded_separately(self):
        self.process()
        self.assertTrue(self.pdf.is_file())
        self.assertIn('Delivery PDF pipeline test',self.txt.read_text())
        self.assertEqual(self.finish.call_args.kwargs['status'],'done')
        self.assertEqual(self.finish.call_args.kwargs['pdf_status'],'done')
        self.pdf_update.assert_called_once()
        self.text_update.assert_called_once_with(self.conn,42,extracted=True)

    def test_cancellation_discards_staged_file(self):
        self.owns.return_value=False
        self.process()
        self.assertFalse(self.pdf.exists())
        self.pdf_update.assert_not_called()
        self.finish.assert_not_called()
        self.assertEqual(list((self.root/'.file-work').iterdir()),[])

    def test_pdf_success_survives_text_failure(self):
        def execute(stage,*args,**kwargs):
            return self.executor(stage,*args,**kwargs) if stage=='pdf' else {'success':False,'error':'no_text_layer'}
        self.process(execute)
        self.assertTrue(self.pdf.is_file())
        self.assertFalse(self.txt.exists())
        self.assertEqual(self.finish.call_args.kwargs['pdf_status'],'done')
        self.assertEqual(self.finish.call_args.kwargs['text_status'],'failed')
        self.assertFalse(self.finish.call_args.kwargs['retry'])

    def test_retryable_download_failure_does_not_claim_text_failure(self):
        self.process(lambda *a,**kw:{'success':False,'error':'download_failed'})
        self.assertEqual(self.finish.call_args.kwargs['text_status'],'pending')
        self.assertTrue(self.finish.call_args.kwargs['retry'])
        self.assertFalse(self.pdf.exists())

    def test_good_existing_files_are_reused(self):
        self.pdf.parent.mkdir(parents=True)
        self.pdf.write_bytes(make_pdf())
        self.txt.write_text('Existing text')
        self.doc.update(pdf_downloaded=1,text_extracted=1,pdf_sha256=hashlib.sha256(make_pdf()).hexdigest())
        executor=mock.Mock(side_effect=AssertionError('no new processing needed'))
        self.process(executor)
        executor.assert_not_called()
        self.assertEqual(self.finish.call_args.kwargs['status'],'done')

    def test_missing_pdf_url_is_skipped_without_downloading(self):
        self.doc['pdf_url']=None
        self.process(mock.Mock(side_effect=AssertionError('no URL')))
        self.assertEqual(self.finish.call_args.kwargs['status'],'skipped')
        self.assertEqual(self.finish.call_args.kwargs['pdf_status'],'unavailable')

    def test_low_disk_space_is_visible_and_bounded_by_retry_policy(self):
        from types import SimpleNamespace
        with mock.patch.object(worker.shutil,'disk_usage',return_value=SimpleNamespace(free=1)):
            self.process(mock.Mock(side_effect=AssertionError('must not download')))
        self.assertEqual(self.finish.call_args.kwargs['error'],'disk_space_low')
        self.assertTrue(self.finish.call_args.kwargs['retry'])

    def _pdf_stage(self, routes, url='https://fixture.test/docs/file.pdf', resolve=None):
        """download_pdf through the real Fetcher with a fake transport. Returns (result, requests seen)."""
        from crawler.enumeration.http import Response
        from delivery.files.stage import download_pdf
        seen = []
        class Transport:
            def __init__(self):
                if resolve:
                    self.resolve = resolve
            def request(self, method, target, *, headers=None):
                seen.append((target, (headers or {}).get('User-Agent')))
                hit = routes.get(target)
                return hit if hit is not None else Response(404, {}, b'', target)
        task = self.root / 'task-1'
        task.mkdir(exist_ok=True)
        return download_pdf(task, url, transport=Transport()), seen, task

    def test_pdf_stage_downloads_actual_bytes_and_rejects_html(self):
        from crawler.enumeration.http import Response
        robots = Response(200, {'content-type': 'text/plain'}, b'User-agent: *\nAllow: /\n', 'https://fixture.test/robots.txt')
        for content, expected in [(make_pdf(), True), (b'<html>Login</html>', False)]:
            with self.subTest(expected=expected):
                result, seen, task = self._pdf_stage({'https://fixture.test/robots.txt': robots,
                    'https://fixture.test/docs/file.pdf': Response(200, {'content-type': 'application/pdf'}, content, '')})
                self.assertEqual(result['success'], expected)
                if expected:
                    self.assertEqual((task / 'download.pdf').read_bytes(), content)
                    self.assertEqual(result['size'], len(content))
                else:
                    self.assertEqual(result['error'], 'invalid_pdf')
                # robots.txt first, then the file, both with the collector's honest User-Agent
                self.assertEqual([u for u, _ in seen], ['https://fixture.test/robots.txt', 'https://fixture.test/docs/file.pdf'])
                self.assertTrue(all(ua and ua.startswith('LibertreeCollector/') for _, ua in seen))

    def test_pdf_stage_honours_robots_and_access_refusals(self):
        from crawler.enumeration.http import Response
        url = 'https://fixture.test/docs/file.pdf'
        disallow = Response(200, {'content-type': 'text/plain'}, b'User-agent: *\nDisallow: /docs/\n', '')
        result, seen, _ = self._pdf_stage({'https://fixture.test/robots.txt': disallow})
        self.assertEqual(result['error'], 'robots_disallowed')
        self.assertNotIn(url, [u for u, _ in seen])  # never requested
        allow = Response(200, {'content-type': 'text/plain'}, b'User-agent: *\nAllow: /\n', '')
        result, _, _ = self._pdf_stage({'https://fixture.test/robots.txt': allow, url: Response(403, {}, b'denied', url)})
        self.assertEqual(result['error'], 'access_blocked')
        result, _, _ = self._pdf_stage({'https://fixture.test/robots.txt': allow, url: Response(429, {}, b'', url)})
        self.assertEqual(result['error'], 'rate_limited')
        # the 429 leaves a cooldown in the shared host gate: the next request to that host is refused too
        result, seen, _ = self._pdf_stage({'https://fixture.test/robots.txt': allow, url: Response(200, {}, make_pdf(), url)})
        self.assertEqual((result['error'], seen), ('rate_limited', []))
        other = 'https://other.test/file.pdf'
        result, _, _ = self._pdf_stage({'https://other.test/robots.txt': allow}, url=other)  # the file is a 404
        self.assertEqual((result['error'], result.get('permanent')), ('download_failed', True))

    def test_refused_downloads_are_not_retried(self):
        for code in ('robots_disallowed', 'robots_unavailable', 'access_blocked', 'rate_limited'):
            with self.subTest(code=code):
                self.process(mock.Mock(return_value={'success': False, 'error': code}))
                self.assertEqual(self.finish.call_args.kwargs['error'], code)
                self.assertFalse(self.finish.call_args.kwargs['retry'])
        self.process(mock.Mock(return_value={'success': False, 'error': 'download_failed'}))
        self.assertTrue(self.finish.call_args.kwargs['retry'])
        # the Fetcher reason is kept with the code; a permanent answer (404) is not retried
        self.process(mock.Mock(return_value={'success': False, 'error': 'download_failed', 'reason': 'http_404', 'status': 404, 'permanent': True}))
        self.assertEqual(self.finish.call_args.kwargs['error'], 'download_failed:http_404:404')
        self.assertFalse(self.finish.call_args.kwargs['retry'])

    def test_pdf_stage_rejects_private_destination_before_request(self):
        result, seen, _ = self._pdf_stage({}, url='http://fixture.test/file.pdf', resolve=lambda host, port=None: ['127.0.0.1'])
        self.assertFalse(result['success'])
        self.assertEqual(seen, [])
        result, seen, _ = self._pdf_stage({}, url='ftp://fixture.test/file.pdf')
        self.assertEqual((result['error'], seen), ('download_failed', []))


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires disposable PostgreSQL')
class FilePipelinePostgresTest(unittest.TestCase):
    def test_existing_document_backfill_preview_enqueue_and_active_exclusion(self):
        from delivery.files import jobs
        seqs=[self.db.insert_document(self.conn,{'site_id':'file-fixture','title':str(i),
              'meta_url':f'https://fixture.invalid/{i}', 'pdf_url':f'https://fixture.invalid/{i}.pdf'}) for i in range(5)]
        from datetime import date
        self.conn.execute("UPDATE documents SET collected_at='2026-10-08 23:59:59+00'")
        self.conn.commit()
        self.assertEqual(jobs.backfill(self.conn,'file-fixture',collected_to=date(2026,10,7))['selected'],0)
        self.assertEqual(jobs.backfill(self.conn,'file-fixture',collected_from=date(2026,10,8),collected_to=date(2026,10,8))['selected'],5)
        preview=jobs.backfill(self.conn,'file-fixture',limit=3)
        self.assertEqual(preview['selected'],3)
        self.assertEqual(self.conn.execute('SELECT count(*) AS n FROM crawl_jobs').fetchone()['n'],0)
        queued=jobs.backfill(self.conn,'file-fixture',limit=3,dry_run=False)
        parent=self.conn.execute('SELECT * FROM crawl_jobs WHERE id=%s',(queued['job_id'],)).fetchone()
        self.assertEqual(parent['job_type'],'file_backfill')
        self.assertEqual(parent['status'],'done')
        self.assertEqual(jobs.backfill(self.conn,'file-fixture',limit=3)['selected'],2)
        self.assertEqual(jobs.claim(self.conn)['seq_id'],seqs[0])
        self.assertEqual(self.conn.execute('SELECT count(*) AS n FROM documents').fetchone()['n'],5)

    def setUp(self):
        from crawler import db_pg
        from delivery.db.schema import init_delivery_schema
        self.db=db_pg
        self.conn=db_pg.open_db(os.environ['TEST_PG_DSN'])
        self.conn.execute('DROP SCHEMA public CASCADE')
        self.conn.execute('CREATE SCHEMA public')
        self.conn.commit()
        db_pg.init_db(self.conn)
        init_delivery_schema(self.conn)
        db_pg.upsert_site(self.conn,'file-fixture','File Fixture','https://fixture.invalid')
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.conn.close)

    def queue(self,url='https://fixture.invalid/sample.pdf'):
        from delivery.worker import jobs as crawl_jobs
        from delivery.files import jobs
        seq=self.db.insert_document(self.conn,{'site_id':'file-fixture','title':'PDF fixture',
            'meta_url':'https://fixture.invalid/1','pdf_url':url})
        jid=crawl_jobs.enqueue_job(self.conn,'file-fixture',process_files=True)
        crawl_jobs.claim_next_job(self.conn)
        jobs.enqueue(self.conn,jid,seq)
        self.assertIsNone(jobs.claim(self.conn))  # Wait for metadata completion.
        crawl_jobs.finish_job(self.conn,jid,1)
        return jid,seq

    def execute(self,stage,directory,url=None,**kwargs):
        directory=Path(directory)
        if stage=='pdf':
            content=make_pdf()
            (directory/'download.pdf').write_bytes(content)
            return {'success':True,'sha256':hashlib.sha256(content).hexdigest(),'size':len(content)}
        return worker.run_stage(stage,directory,timeout=20)

    def test_download_extract_retry_and_reuse_are_durable(self):
        from delivery.files import jobs
        jid,seq=self.queue()
        task=jobs.claim(self.conn)
        worker.process(self.conn,task,root=self.temp.name,executor=self.execute)
        report=jobs.summary(self.conn,jid)
        self.assertEqual((report['done'],report['pdf_done'],report['text_done']),(1,1,1))
        doc=self.db.get_document(self.conn,seq)
        self.assertEqual((doc['pdf_downloaded'],doc['text_extracted']),(1,1))
        self.conn.execute("UPDATE document_file_jobs SET status='failed'")
        self.conn.commit()
        self.assertEqual(jobs.retry_failed(self.conn,jid),1)
        with mock.patch.object(worker,'run_stage',side_effect=AssertionError('must reuse existing files')) as execute:
            worker.process(self.conn,jobs.claim(self.conn),root=self.temp.name,executor=execute)
        self.assertEqual(jobs.summary(self.conn,jid)['done'],1)

    def test_missing_url_is_skipped_and_duplicate_enqueue_is_idempotent(self):
        from delivery.files import jobs
        jid,seq=self.queue(url=None)
        jobs.enqueue(self.conn,jid,seq)
        worker.process(self.conn,jobs.claim(self.conn),root=self.temp.name,executor=self.execute)
        report=jobs.summary(self.conn,jid)
        self.assertEqual((report['total'],report['skipped']),(1,1))
        self.assertEqual(report['items'][0]['error'],'no_pdf_url')

    def test_download_failure_retries_three_times_and_then_stops(self):
        from delivery.files import jobs
        jid,seq=self.queue()
        for attempt in range(1,4):
            worker.process(self.conn,jobs.claim(self.conn),root=self.temp.name,
                           executor=lambda *a,**kw:{'success':False,'error':'download_failed'})
            report=jobs.summary(self.conn,jid)
            self.assertEqual(report['queued' if attempt<3 else 'failed'],1)
            self.conn.execute('UPDATE document_file_jobs SET next_attempt_at=now()')
            self.conn.commit()
        self.assertEqual(self.db.get_document(self.conn,seq)['pdf_downloaded'],0)

    def test_cancel_during_download_prevents_publication(self):
        from delivery.files import jobs
        jid,seq=self.queue()
        def cancelling(stage,directory,url=None,**kwargs):
            result=self.execute(stage,directory,url,**kwargs)
            jobs.cancel(self.conn,jid)
            return result
        worker.process(self.conn,jobs.claim(self.conn),root=self.temp.name,executor=cancelling)
        self.assertFalse(worker.blob_storage.get_blob_path(seq,root=self.temp.name).exists())
        self.assertEqual(jobs.summary(self.conn,jid)['cancelled'],1)

    def test_no_text_preserves_pdf_success(self):
        from delivery.files import jobs
        jid,seq=self.queue()
        def no_text(stage,*args,**kwargs):
            return self.execute(stage,*args,**kwargs) if stage=='pdf' else {'success':False,'error':'no_text_layer'}
        worker.process(self.conn,jobs.claim(self.conn),root=self.temp.name,executor=no_text)
        report=jobs.summary(self.conn,jid)
        self.assertEqual((report['failed'],report['pdf_done'],report['text_done']),(1,1,0))
        self.assertEqual(report['items'][0]['error'],'no_text_layer')

    def test_expired_lease_recovers_and_old_token_cannot_finish(self):
        from delivery.files import jobs
        jid,seq=self.queue()
        task=jobs.claim(self.conn)
        self.conn.execute("UPDATE document_file_jobs SET lease_expires_at=now()-interval '1 minute'")
        self.conn.commit()
        jobs.recover(self.conn)
        jobs.finish(self.conn,task,status='done',pdf_status='done',text_status='done')
        self.assertEqual(jobs.summary(self.conn,jid)['queued'],1)

    def test_crawler_enqueues_both_new_and_known_documents(self):
        from crawler.base_crawler import BaseCrawler
        from delivery.worker import jobs as crawl_jobs, worker as crawl_worker
        from delivery.files import jobs
        class Fixture(BaseCrawler):
            site_id='file-fixture';site_name='Fixture';base_url='https://fixture.invalid'
            def crawl(self,limit=None):
                self._save_paper_v2({'meta_url':'https://fixture.invalid/1','title':'Test','pdf_url':'https://fixture.invalid/file.pdf'})
        with mock.patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres'}):
            for _ in range(2):
                jid=crawl_jobs.enqueue_job(self.conn,'file-fixture',process_files=True)
                crawl_worker.run_job(self.conn,crawl_jobs.claim_next_job(self.conn),{'file-fixture':Fixture},delay=0)
                self.assertEqual(jobs.summary(self.conn,jid)['total'],1)


if __name__=='__main__':
    unittest.main()
