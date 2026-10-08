import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from crawler.blob_storage import get_blob_path
from delivery.scripts import recover_test_data as recovery


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires disposable PostgreSQL')
class TestDataRecoveryTests(unittest.TestCase):
    def setUp(self):
        from crawler import db_pg
        from delivery.be.migrate import migrate
        self.db=db_pg
        self.conn=db_pg.open_db(os.environ['TEST_PG_DSN'])
        self.conn.execute('DROP SCHEMA public CASCADE')
        self.conn.execute('CREATE SCHEMA public')
        self.conn.commit()
        migrate(os.environ['TEST_PG_DSN'])
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)/'blobs';self.root.mkdir()
        self.backup=Path(self.temp.name)/'backup'
        self.site='krivet-re-kr'
        self.ids=[]
        for i in range(2):
            self.ids.append(self.db.insert_document(self.conn,{
                'site_id':self.site,'post_number':f'NH{i}',
                'meta_url':f'https://www.krivet.re.kr/kor/sub.do?menuSn=23&pstNo=NH{i}',
                'title':f'Fixture {i}','published_date':'2026-10-08'}))
        self.normal,self.target=self.ids
        for seq in self.ids:
            for ext,data in [('pdf',b'%PDF-1.4 fixture'),('txt',b'fixture text')]:
                path=get_blob_path(seq,ext,root=self.root);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        self.conn.execute("UPDATE documents SET collected_at='2026-10-08T03:00:00Z'")
        self.conn.execute("INSERT INTO crawl_site_progress(site_id,cursor,items_done,completed_at) VALUES(%s,'{\"page\":5}',20,now())",(self.site,))
        self.job=self.conn.execute("INSERT INTO crawl_jobs(site_id,status,saved_count) VALUES(%s,'done',2) RETURNING id",(self.site,)).fetchone()['id']
        for seq in self.ids:
            self.conn.execute("INSERT INTO document_lang VALUES(%s,'ko')",(seq,))
            self.conn.execute("""INSERT INTO document_translations(seq_id,source_field,target_locale,source_fingerprint,
              model_version,prompt_version,state,translation_text) VALUES(%s,'title','ko-KR',%s,'fixture','v1','completed','translated')""",(seq,'a'*64))
            tid=self.conn.execute("""INSERT INTO translation_jobs(seq_id,source_field,source_fingerprint,
              provider,model_version,prompt_version,status) VALUES(%s,'title',%s,'external','fixture','v1','completed') RETURNING id""",(seq,'a'*64)).fetchone()['id']
            self.conn.execute("INSERT INTO translation_job_attempts(job_id,attempt_no,worker_id,status) VALUES(%s,1,'fixture','completed')",(tid,))
            summary=self.conn.execute("""INSERT INTO document_summaries(seq_id,source_fingerprint,model_version,prompt_version,state,summary_text)
              VALUES(%s,%s,'fixture','v1','completed','summary') RETURNING summary_id""",(seq,'a'*64)).fetchone()['summary_id']
            self.conn.execute("INSERT INTO document_summary_quality(summary_id,gate_version,decision,score) VALUES(%s,'fixture','auto_approved',1)",(summary,))
            self.conn.execute("INSERT INTO document_file_jobs(crawl_job_id,seq_id,status) VALUES(%s,%s,'done')",(self.job,seq))
        self.conn.commit()

    def tearDown(self):
        self.conn.close();self.temp.cleanup()

    def plan(self, **kw):
        args=dict(site_id=self.site,seq_ids=[self.target],collected_from='2026-10-08T00:00:00Z',
                  collected_to='2026-10-09T00:00:00Z',blob_root=self.root,rescan=True)
        args.update(kw)
        return recovery.make_plan(self.conn,**args)

    def run_plan(self,plan,**kw):
        return recovery.run(self.conn,plan,blob_root=self.root,backup_dir=self.backup,workers_stopped=True,**kw)

    def count(self,name):
        count=self.conn.execute(f'SELECT count(*) AS n FROM {name}').fetchone()['n']
        self.conn.rollback()
        return count

    def test_exact_target_graph_files_cursor_restore_and_recollect(self):
        plan=self.plan()
        self.assertEqual(self.count('documents'),2)
        self.assertTrue(get_blob_path(self.target,root=self.root).exists())
        self.run_plan(plan)
        for name in ('documents','document_lang','document_translations','translation_jobs',
                     'translation_job_attempts','document_summaries','document_summary_quality','document_file_jobs'):
            self.assertEqual(self.count(name),1,name)
        self.assertTrue(get_blob_path(self.normal,root=self.root).exists())
        self.assertFalse(get_blob_path(self.target,root=self.root).exists())
        self.assertEqual(self.count('crawl_jobs'),1)
        cursor=self.conn.execute('SELECT * FROM crawl_site_progress').fetchone()
        self.assertEqual(cursor['items_done'],0);self.assertIsNone(cursor['cursor']);self.assertIsNone(cursor['completed_at'])
        self.conn.rollback()
        self.run_plan(plan)  # Reapplying is harmless.
        self.run_plan(plan,restore=True)
        self.run_plan(plan,restore=True)
        self.assertEqual(self.count('documents'),2)
        self.assertEqual(self.count('document_translations'),2)
        self.assertEqual(get_blob_path(self.target,'txt',root=self.root).read_text(),'fixture text')
        self.assertEqual(recovery.file_inventory(self.root,[self.target]),plan['plan']['files'])
        self.assertEqual(self.db.insert_document(self.conn,{'site_id':self.site,'post_number':'NH1',
              'meta_url':'https://www.krivet.re.kr/kor/sub.do?pstNo=NH1&menuSn=23','title':'Recollect'}),self.target)
        self.conn.rollback()
        # After deliberate test-data removal a new collection gets a new ID.
        self.run_plan(plan)
        new=self.db.insert_document(self.conn,{'site_id':self.site,'post_number':'NH1',
              'meta_url':'https://www.krivet.re.kr/kor/sub.do?pstNo=NH1&menuSn=23','title':'Recollect'})
        self.assertGreater(new,self.target)
        self.assertEqual(self.count('documents'),2)
        self.assertFalse(get_blob_path(new,root=self.root).exists())

    def test_crash_after_db_commit_resumes_files(self):
        plan=self.plan()
        with patch.object(recovery,'reconcile_files',side_effect=OSError('injected disk failure')):
            with self.assertRaises(OSError):self.run_plan(plan)
        self.assertEqual(self.count('documents'),1)
        self.assertTrue(get_blob_path(self.target,root=self.root).exists())
        self.run_plan(plan)
        self.assertFalse(get_blob_path(self.target,root=self.root).exists())
        self.run_plan(plan,restore=True)
        self.assertEqual(self.count('documents'),2)

    def test_transaction_failure_preserves_database_and_files(self):
        plan=self.plan()
        with patch.object(recovery,'replace_progress',side_effect=RuntimeError('injected transaction failure')):
            with self.assertRaises(RuntimeError):self.run_plan(plan)
        self.assertEqual(self.count('documents'),2)
        self.assertEqual(self.count('document_translations'),2)
        self.assertTrue(get_blob_path(self.target,root=self.root).exists())
        self.run_plan(plan)

    def test_changed_preview_and_blob_rejected(self):
        plan=self.plan()
        self.conn.execute("UPDATE documents SET title='changed' WHERE seq_id=%s",(self.target,));self.conn.commit()
        with self.assertRaisesRegex(ValueError,'data changed'):self.run_plan(plan)
        self.assertEqual(self.count('documents'),2)
        plan=self.plan()
        # Backup destination belongs to the old plan; a new directory is required.
        self.backup=Path(self.temp.name)/'backup2'
        get_blob_path(self.target,root=self.root).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'blob changed'):self.run_plan(plan)
        self.assertEqual(self.count('documents'),2)

    def test_wrong_scope_jobs_and_unknown_foreign_keys_block(self):
        with self.assertRaisesRegex(ValueError,'do not all match'):
            self.plan(collected_from='2026-10-09T00:00:00Z',collected_to='2026-10-10T00:00:00Z')
        self.conn.execute("UPDATE crawl_jobs SET status='running'");self.conn.commit()
        with self.assertRaisesRegex(ValueError,'jobs must be stopped'):self.plan()
        self.conn.execute("UPDATE crawl_jobs SET status='done'")
        self.conn.execute('CREATE TABLE unknown_reference(id INT,seq_id BIGINT REFERENCES documents(seq_id) ON DELETE CASCADE)');self.conn.commit()
        with self.assertRaisesRegex(ValueError,'unknown document reference'):self.plan()

    def test_missing_old_optional_file_table_supported(self):
        self.conn.execute('DROP TABLE document_file_jobs');self.conn.commit()
        plan=self.plan()
        self.run_plan(plan)
        self.run_plan(plan,restore=True)
        self.assertEqual(self.count('documents'),2)

    def test_restore_refuses_recollected_identity_and_corrupt_backup(self):
        plan=self.plan();self.run_plan(plan)
        self.db.insert_document(self.conn,{'site_id':self.site,'post_number':'NH1',
          'meta_url':'https://www.krivet.re.kr/kor/sub.do?pstNo=NH1&menuSn=42','title':'Recollected'})
        with self.assertRaisesRegex(ValueError,'recollected identity'):self.run_plan(plan,restore=True)
        self.conn.execute('DELETE FROM documents WHERE seq_id<>%s',(self.normal,));self.conn.commit()
        path=self.backup/'files'/plan['plan']['files'][0]['relative']
        path.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError,'incomplete or corrupt'):self.run_plan(plan,restore=True)
        self.assertEqual(self.count('documents'),1)

    def test_schema_and_active_schedule_changes_after_preview_rejected(self):
        plan=self.plan()
        self.conn.execute("INSERT INTO crawl_schedules(site_id,interval_hours,next_run_at) VALUES(%s,24,now())",(self.site,));self.conn.commit()
        with self.assertRaisesRegex(ValueError,'schedules must be disabled'):self.run_plan(plan)
        self.conn.execute('UPDATE crawl_schedules SET enabled=false')
        self.conn.execute('ALTER TABLE documents ADD COLUMN customer_custom TEXT');self.conn.commit()
        with self.assertRaisesRegex(ValueError,'schema differs'):self.run_plan(plan)
        self.assertEqual(self.count('documents'),2)

    def test_interrupted_file_restore_resumes(self):
        plan=self.plan();self.run_plan(plan)
        actual_copy=recovery.shutil.copyfileobj
        calls=0
        def interrupted(source,target):
            nonlocal calls
            calls+=1
            if calls==2:
                target.write(b'partial')
                raise OSError('injected interrupted restore')
            return actual_copy(source,target)
        with patch.object(recovery.shutil,'copyfileobj',side_effect=interrupted):
            with self.assertRaises(OSError):self.run_plan(plan,restore=True)
        self.assertEqual(self.count('documents'),1)
        self.run_plan(plan,restore=True)
        self.assertEqual(self.count('documents'),2)
        self.assertEqual(recovery.file_inventory(self.root,[self.target]),plan['plan']['files'])
