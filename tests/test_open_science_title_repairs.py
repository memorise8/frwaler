"""Literal catalogue titles and reversible, source-bound title-only repairs."""
import copy,os,sqlite3,unittest
from crawler import db_libertree,db_pg
from crawler.open_science_titles import source_title,legacy_title
from delivery.scripts.repair_metadata import execute,rows,digest
from delivery.scripts import repair_open_science_titles as repair
from tests.test_coupled_site_counts import CoupledContract
from tests.test_open_science_delivery_scopes import OpenScienceDeliveryScopes

class LiteralTitles(CoupledContract,unittest.TestCase):
    SITE='open-science-canada-ca-communities';LIST_METHOD='_fetch_list';LIST_MODULE=False
    ORIGINAL=OpenScienceDeliveryScopes.ORIGINAL;ADDITIONAL=OpenScienceDeliveryScopes.ADDITIONAL
    crawler=OpenScienceDeliveryScopes.crawler
    def item(self,n):
        item=OpenScienceDeliveryScopes.item(self,n);item['metadata']['dc.title'][0]['value']=f'Yield*Trait # {n}: C# x_y > 0';return item
    def test_native_insertion_keeps_symbols_and_repeat_keeps_rows(self):
        c=self.crawler();self.assertEqual(c.crawl(),8)
        for r in rows(self.conn,'SELECT post_number,title FROM documents'):
            self.assertEqual(r['title'],f"Yield*Trait # {int(r['post_number'][:8])}: C# x_y > 0")
        self.conn.rollback();before=self.identities();self.assertEqual(self.crawler().crawl(),0);self.assertEqual(self.identities(),before)
    def test_resume_recognizes_legacy_IDs_without_rewriting_historical_titles(self):
        c=self.crawler();c.delivery_mode='backfill';self.assertEqual(c.crawl(limit=3),3);cursor=c.delivery_pending_cursor
        execute(self.conn,'UPDATE documents SET title=? WHERE post_number=?',('Yield Trait 1: C x y 0',self.item(1)['uuid']));self.conn.commit();before=rows(self.conn,'SELECT * FROM documents ORDER BY seq_id');self.conn.rollback();c=self.crawler();c.delivery_mode='backfill';c.delivery_cursor=cursor;self.assertEqual(c.crawl(),5);self.assertTrue(c.delivery_exhausted);self.assertEqual(rows(self.conn,'SELECT * FROM documents ORDER BY seq_id')[:3],before);self.conn.rollback()
    def test_language_order_inline_markup_and_name_fallback_keep_literal_symbols(self):
        item=dict(name='Fallback #1',metadata={'dc.title':[{'value':'Français','language':'fr','place':0},{'value':'A <fc>species</fc> &amp; Yield*Trait #1','language':'en','place':2}]})
        self.assertEqual(source_title(item),'A species & Yield*Trait #1');self.assertEqual(legacy_title(item),'A species & Yield Trait 1')
        self.assertEqual(source_title(dict(name='Fallback #1',metadata={})), 'Fallback #1')

@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class LiteralTitlesPostgres(LiteralTitles):BACKEND='postgres'

class TitleRepair(unittest.TestCase):
    BACKEND='sqlite'
    def setUp(self):
        if self.BACKEND=='postgres':
            self.conn=db_pg.open_db(os.environ['TEST_PG_DSN']);self.conn.execute('DROP SCHEMA public CASCADE');self.conn.execute('CREATE SCHEMA public');self.conn.commit();self.backend=db_pg
        else:self.conn=sqlite3.connect(':memory:');self.backend=db_libertree
        self.addCleanup(self.conn.close);self.backend.init_db(self.conn);self.backend.upsert_site(self.conn,repair.SITE,'OpenScience',repair.BASE);self.evidence=dict(source_manifest_sha256='a'*64,records=[])
    def seed(self,n=1):
        uid=f'{n:08d}-0000-0000-0000-000000000000';url=repair.BASE+'/handle/123456789/'+str(n);item=dict(uuid=uid,type='item',name=f'Yield*Trait #{n}',handle=f'123456789/{n}',metadata={'dc.title':[dict(value=f'Yield*Trait #{n}',language='en')]},_links={'self':{'href':repair.BASE+'/server/api/core/items/'+uid}})
        seq=self.backend.insert_document(self.conn,dict(site_id=repair.SITE,post_number=uid,meta_url=url,title=legacy_title(item),abstract='Retained source body',summary='Retained summary',published_date='2026-08-31',pdf_url=repair.BASE+'/report.pdf'));self.conn.commit();self.evidence['records'].append(dict(post_number=uid,meta_url=url,source_sha256='b'*64,source_item=item));return seq
    def snapshot(self):
        result=rows(self.conn,'SELECT * FROM documents ORDER BY seq_id');self.conn.rollback();return result
    def plan(self):
        result=repair.make_plan(self.conn,self.evidence);self.conn.rollback();return result
    def test_apply_repeat_restore_reapply_preserves_all_other_columns(self):
        self.seed();execute(self.conn,'UPDATE documents SET pdf_downloaded=1,text_extracted=1,pdf_sha256=?,pdf_size_bytes=123',('c'*64,));self.conn.commit();before=self.snapshot();plan=self.plan();self.assertEqual(repair.apply_plan(self.conn,plan),1);after=self.snapshot();expected=copy.deepcopy(before);expected[0]['title']='Yield*Trait #1';self.assertEqual(after,expected);self.assertEqual(repair.apply_plan(self.conn,plan),0);self.assertEqual(self.plan()['plan']['changes'],[]);self.assertEqual(repair.apply_plan(self.conn,plan,restore=True),1);self.assertEqual(self.snapshot(),before);self.assertEqual(repair.apply_plan(self.conn,plan,restore=True),0);self.assertEqual(repair.apply_plan(self.conn,plan),1)
    def test_stale_second_title_rolls_back_entire_batch(self):
        self.seed();second=self.seed(2);plan=self.plan();execute(self.conn,'UPDATE documents SET title=? WHERE seq_id=?',('Manual title',second));self.conn.commit();before=self.snapshot()
        with self.assertRaisesRegex(ValueError,'title changed'):repair.apply_plan(self.conn,plan)
        self.assertEqual(self.snapshot(),before)
    def test_changed_unrelated_field_rejects_stale_plan(self):
        self.seed();plan=self.plan();execute(self.conn,'UPDATE documents SET summary=?',('Concurrent summary',));self.conn.commit();before=self.snapshot()
        with self.assertRaises(ValueError):repair.apply_plan(self.conn,plan)
        self.assertEqual(self.snapshot(),before)
    def test_arbitrary_or_already_correct_titles_are_not_overwritten(self):
        self.seed();execute(self.conn,'UPDATE documents SET title=?',('Curated title',));self.conn.commit();plan=self.plan();self.assertTrue(plan['plan']['unresolved'])
        with self.assertRaises(ValueError):repair.apply_plan(self.conn,plan)
        execute(self.conn,'UPDATE documents SET title=?',('Yield*Trait #1',));self.conn.commit();self.assertEqual(self.plan()['plan']['changes'],[])
    def test_changed_source_binding_and_tampered_target_are_rejected(self):
        self.seed();plan=self.plan();bad=copy.deepcopy(plan);bad['plan']['changes'][0]['after']='Unrelated title';bad['sha256']=digest(bad['plan'])
        with self.assertRaises(ValueError):repair.apply_plan(self.conn,bad)
        self.evidence['records'][0]['meta_url']='https://outside.invalid/handle/123456789/1'
        with self.assertRaises(ValueError):self.plan()
    def test_identity_collision_is_held_at_preview_and_apply(self):
        self.seed();plan=self.plan();r=self.snapshot()[0];execute(self.conn,'INSERT INTO documents(site_id,post_number,meta_url,title) VALUES(?,?,?,?)',(repair.SITE,r['post_number'],r['meta_url']+'?alias=1','Duplicate'));self.conn.commit();self.assertTrue(self.plan()['plan']['unresolved'])
        with self.assertRaises(ValueError):repair.apply_plan(self.conn,plan)
    def test_other_site_is_not_selected_and_batches_are_bounded(self):
        self.seed();self.backend.upsert_site(self.conn,'other','Other','https://example.invalid');execute(self.conn,'UPDATE documents SET site_id=?',('other',));self.conn.commit();self.assertEqual(self.plan()['plan']['changes'],[])
        for limit in [True,0,1001]:
            with self.assertRaises(ValueError):repair.make_plan(self.conn,self.evidence,limit=limit)
    def test_duplicate_source_evidence_is_rejected(self):
        self.seed();self.evidence['records'].append(copy.deepcopy(self.evidence['records'][0]))
        with self.assertRaises(ValueError):self.plan()

@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class TitleRepairPostgres(TitleRepair):
    BACKEND='postgres'
    def test_active_metadata_job_holds_existing_plan(self):
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs
        self.seed();init_delivery_schema(self.conn);plan=self.plan();jobs.enqueue_job(self.conn,repair.SITE,mode='full')
        with self.assertRaisesRegex(ValueError,'active metadata'):repair.apply_plan(self.conn,plan)
        self.assertTrue(self.plan()['plan']['unresolved'])
    def test_translations_file_jobs_and_metadata_jobs_keep_their_references(self):
        from delivery.db.schema import init_catalogue_schema,init_delivery_schema
        from delivery.worker import jobs
        from delivery.files import jobs as files
        seq=self.seed();init_catalogue_schema(self.conn);init_delivery_schema(self.conn);jid=jobs.enqueue_job(self.conn,repair.SITE,mode='full');jobs.claim_next_job(self.conn);files.enqueue(self.conn,jid,seq);self.conn.execute("UPDATE crawl_jobs SET status='done'");self.conn.execute("UPDATE document_file_jobs SET status='failed'");self.conn.execute("INSERT INTO document_translations(seq_id,source_field,target_locale,source_fingerprint,model_version,prompt_version,state,translation_text) VALUES (%s,'title','en-US',%s,'fixture-model','fixture-prompt','completed','Retained translation')",(seq,'d'*64));self.conn.commit();tables=['document_translations','document_file_jobs','crawl_jobs'];before={t:rows(self.conn,'SELECT * FROM '+t) for t in tables};self.conn.rollback();plan=self.plan();self.assertEqual(repair.apply_plan(self.conn,plan),1);after={t:rows(self.conn,'SELECT * FROM '+t) for t in tables};self.conn.rollback();self.assertEqual(after,before)
