"""Bounded attachment-only repairs, stale plans, rollback and retained references."""
import copy,os,sqlite3,unittest
from crawler import db_libertree,db_pg
from delivery.scripts.repair_metadata import execute,rows
from delivery.scripts import repair_krivet_attachments as repair

class AttachmentRepair(unittest.TestCase):
    BACKEND='sqlite'
    def setUp(self):
        if self.BACKEND=='postgres':
            self.conn=db_pg.open_db(os.environ['TEST_PG_DSN']);self.conn.execute('DROP SCHEMA public CASCADE');self.conn.execute('CREATE SCHEMA public');self.conn.commit();self.backend=db_pg
        else:self.conn=sqlite3.connect(':memory:');self.backend=db_libertree
        self.addCleanup(self.conn.close);self.backend.init_db(self.conn);self.backend.upsert_site(self.conn,repair.SITE,'KRIVET','https://www.krivet.re.kr');self.evidence=dict(source_manifest_sha256='a'*64,records=[])
    def seed(self,uid='PB0000000001'):
        url='https://www.krivet.re.kr/kor/sub.do?menuSn=15&pstNo='+uid
        old='https://www.krivet.re.kr'+repair.OLD_PATH+'?fileDownDiv=NOTICOMM&pstNo='+uid+'&pstgAtchNo=1'
        new=old.replace(repair.OLD_PATH,repair.NEW_PATH)+'&fileName=Original'
        seq=self.backend.insert_document(self.conn,dict(site_id=repair.SITE,post_number=uid,meta_url=url,title='Retained title',abstract='Retained source abstract',summary='Retained summary',published_date='2026-08-31',pdf_url=old));self.conn.commit()
        self.evidence['records'].append(dict(post_number=uid,source_url=url,source_sha256='b'*64,pdf_url=new));return seq
    def snapshot(self):
        data=rows(self.conn,'SELECT * FROM documents ORDER BY seq_id');self.conn.rollback();return data
    def plan(self):
        plan=repair.make_plan(self.conn,self.evidence);self.conn.rollback();return plan
    def test_apply_repeat_restore_reapply_changes_only_pdf_url(self):
        seq=self.seed();before=self.snapshot();plan=self.plan();self.assertEqual(len(plan['plan']['changes']),1)
        self.assertEqual(repair.apply_plan(self.conn,plan),1);after=self.snapshot();expected=copy.deepcopy(before);expected[0]['pdf_url']=self.evidence['records'][0]['pdf_url'];self.assertEqual(after,expected)
        self.assertEqual(repair.apply_plan(self.conn,plan),0);self.assertEqual(self.plan()['plan']['changes'],[])
        self.assertEqual(repair.apply_plan(self.conn,plan,restore=True),1);self.assertEqual(self.snapshot(),before);self.assertEqual(repair.apply_plan(self.conn,plan,restore=True),0);self.assertEqual(repair.apply_plan(self.conn,plan),1)
        self.assertEqual(self.backend.find_by_dedup_key(self.conn,repair.SITE,before[0]['post_number'],before[0]['meta_url']),seq);self.conn.rollback()
    def test_stale_second_row_rolls_back_whole_batch(self):
        self.seed();second=self.seed('PB0000000002');plan=self.plan();execute(self.conn,'UPDATE documents SET title=? WHERE seq_id=?',('Concurrent title',second));self.conn.commit();before=self.snapshot()
        with self.assertRaisesRegex(ValueError,'changed after preview'):repair.apply_plan(self.conn,plan)
        self.assertEqual(self.snapshot(),before)
    def test_changed_attachment_and_checksum_tampering_hold(self):
        self.seed();plan=self.plan();altered=copy.deepcopy(plan);altered['plan']['changes'][0]['after']+='&other=1'
        with self.assertRaises(ValueError):repair.apply_plan(self.conn,altered)
        execute(self.conn,'UPDATE documents SET pdf_url=?',('https://www.krivet.re.kr/other.pdf',));self.conn.commit()
        with self.assertRaisesRegex(ValueError,'attachment changed'):repair.apply_plan(self.conn,plan)
    def test_reused_source_identity_is_held_at_preview_and_apply(self):
        self.seed();plan=self.plan();row=self.snapshot()[0]
        execute(self.conn,'INSERT INTO documents(site_id,post_number,meta_url,title) VALUES(?,?,?,?)',(repair.SITE,'legacy-id',row['meta_url']+'&extra=1','Duplicate'));self.conn.commit()
        self.assertTrue(self.plan()['plan']['unresolved'])
        with self.assertRaisesRegex(ValueError,'collision'):repair.apply_plan(self.conn,plan)
        self.assertEqual(len(self.snapshot()),2)
    def test_existing_download_flags_and_hashes_require_separate_review(self):
        self.seed();execute(self.conn,'UPDATE documents SET pdf_downloaded=1,pdf_sha256=?',('c'*64,));self.conn.commit();before=self.snapshot();plan=self.plan();self.assertIn('file artifact',plan['plan']['unresolved'][0]['reason'])
        with self.assertRaises(ValueError):repair.apply_plan(self.conn,plan)
        self.assertEqual(self.snapshot(),before)
    def test_unknown_evidence_and_wrong_attachment_identity_cannot_be_guessed(self):
        self.seed();self.evidence['records'][0]['pdf_url']=self.evidence['records'][0]['pdf_url'].replace('pstgAtchNo=1','pstgAtchNo=2');plan=self.plan();self.assertTrue(plan['plan']['unresolved']);self.assertFalse(plan['plan']['changes'])
        for bad in ['https://outside.invalid/com/file/fileDown.do?fileDownDiv=NOTICOMM&pstNo=PB0000000001&pstgAtchNo=1',self.evidence['records'][0]['pdf_url']+'&pstNo=OTHER']:
            self.evidence['records'][0]['pdf_url']=bad
            with self.assertRaises(ValueError):self.plan()
    def test_row_identity_mismatch_is_not_repaired_or_merged(self):
        self.seed();execute(self.conn,'UPDATE documents SET post_number=?',('incorrect-legacy-id',));self.conn.commit();plan=self.plan();self.assertTrue(plan['plan']['unresolved']);self.assertFalse(plan['plan']['changes'])
    def test_other_site_is_never_selected(self):
        self.seed();self.backend.upsert_site(self.conn,'other','Other','https://example.invalid');execute(self.conn,'UPDATE documents SET site_id=?',('other',));self.conn.commit();self.assertEqual(self.plan()['plan']['changes'],[])
    def test_separate_generic_krivet_collector_is_not_modified(self):
        self.seed();self.backend.upsert_site(self.conn,'krivet-re-kr','Generic KRIVET','https://www.krivet.re.kr')
        execute(self.conn,'UPDATE documents SET site_id=?',('krivet-re-kr',));self.conn.commit();self.assertEqual(self.plan()['plan']['changes'],[])
    def test_batch_requires_bounded_integer_limits(self):
        self.seed()
        for limit in [True,0,1001]:
            with self.assertRaises(ValueError):repair.make_plan(self.conn,self.evidence,limit=limit)
    def test_missing_row_holds_without_recreation(self):
        self.seed();plan=self.plan();execute(self.conn,'DELETE FROM documents');self.conn.commit()
        with self.assertRaisesRegex(ValueError,'missing after preview'):repair.apply_plan(self.conn,plan)
        self.assertEqual(self.snapshot(),[])

@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class AttachmentRepairPostgres(AttachmentRepair):
    BACKEND='postgres'
    def file_job(self,seq):
        from delivery.db.schema import init_catalogue_schema,init_delivery_schema
        from delivery.worker import jobs
        from delivery.files import jobs as file_jobs
        init_catalogue_schema(self.conn);init_delivery_schema(self.conn);jid=jobs.enqueue_job(self.conn,repair.SITE,mode='backfill',process_files=True);jobs.claim_next_job(self.conn);file_jobs.enqueue(self.conn,jid,seq)
    def test_new_active_file_job_holds_previewed_repair(self):
        from delivery.db.schema import init_catalogue_schema,init_delivery_schema
        init_catalogue_schema(self.conn);init_delivery_schema(self.conn)
        seq=self.seed();plan=self.plan();self.file_job(seq)
        with self.assertRaisesRegex(ValueError,'active file job'):repair.apply_plan(self.conn,plan)
        current=self.plan();self.assertIn('active file job',current['plan']['unresolved'][0]['reason'])
    def test_completed_translation_and_failed_file_job_links_are_unchanged(self):
        seq=self.seed();self.file_job(seq);self.conn.execute("UPDATE document_file_jobs SET status='failed'")
        self.conn.execute("INSERT INTO document_translations(seq_id,source_field,target_locale,source_fingerprint,model_version,prompt_version,state,translation_text) VALUES (%s,'title','en-US',%s,'fixture-model','fixture-prompt','completed','Retained translation')",(seq,'d'*64));self.conn.commit()
        tables=['document_translations','document_file_jobs','crawl_jobs'];before={t:rows(self.conn,'SELECT * FROM '+t) for t in tables};self.conn.rollback();plan=self.plan();self.assertEqual(repair.apply_plan(self.conn,plan),1)
        after={t:rows(self.conn,'SELECT * FROM '+t) for t in tables};self.conn.rollback();self.assertEqual(after,before)
