import contextlib
import io
import os
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from crawler import db_libertree
from crawler.generic_crawler import GenericCrawler
from delivery.scripts.audit_documents import audit
from delivery.scripts.repair_metadata import execute


class AuditContract:
    def seed(self):
        for site in ('krivet-re-kr','outside-catalogue'):
            self.backend.upsert_site(self.conn,site,site,'https://fixture.invalid')
        for post,url in [('23','https://www.krivet.re.kr/kor/sub.do?menuSn=23&pstNo=NH1'),
                         ('23','https://www.krivet.re.kr/kor/sub.do?menuSn=23&pstNo=NH2')]:
            execute(self.conn,'INSERT INTO documents(site_id,post_number,meta_url,title,published_date) VALUES(?,?,?,?,?)',
                    ('krivet-re-kr',post,url,'Fixture','07.08.2026'))
        for post,url in [('1','https://fixture.invalid/board-a/1'),('1','https://fixture.invalid/board-b/1'),('  ','')]:
            execute(self.conn,'INSERT INTO documents(site_id,post_number,meta_url,title,published_date) VALUES(?,?,?,?,?)',
                    ('outside-catalogue',post,url,'Fixture','2026-02-29'))
        self.conn.commit()

    def test_every_stored_site_and_row_without_auto_classifying_duplicates(self):
        self.seed();events=[]
        result=audit(self.conn,emit=events.append)
        self.assertEqual(result['total_documents'],5)
        self.assertEqual(result['total_sites'],2)
        self.assertEqual(result['sites']['krivet-re-kr']['confirmed_wrong_key'],2)
        self.assertEqual(result['sites']['outside-catalogue']['source_identity_rule_unverified'],3)
        self.assertEqual(result['sites']['outside-catalogue']['missing_or_blank_id'],1)
        self.assertEqual(result['duplicate_key_groups'],2)
        self.assertTrue(all(e['classification']=='requires_site_rule_and_source_review' for e in events if e['kind']=='duplicate_key_candidate'))
        self.assertEqual(result['source_completeness'],'unverified_outside_supplied_manifest')
        # A second scan observes the same data: the audit performed no repair.
        self.assertEqual(audit(self.conn)['sites'],result['sites'])

    def test_source_manifest_distinguishes_present_wrong_id_from_missing(self):
        self.seed()
        manifest={'site_id':'krivet-re-kr','scope':'captured page 1, not entire website','observed_at':'2026-10-08T00:00:00Z',
                  'documents':[{'post_number':f'NH{i}','meta_url':f'https://www.krivet.re.kr/kor/sub.do?pstNo=NH{i}&menuSn=23'} for i in (1,2,3)]}
        result=audit(self.conn,source_manifest=manifest)
        self.assertEqual(result['source_comparison']['results'],{'present':2,'missing_in_reviewed_source_scope':1})


class SQLiteAuditTests(AuditContract,unittest.TestCase):
    def setUp(self):
        self.backend=db_libertree;self.conn=sqlite3.connect(':memory:');self.backend.init_db(self.conn)
    def tearDown(self):self.conn.close()

    def test_overlap_rescan_fills_missing_without_deleting_normal_documents(self):
        self.backend.upsert_site(self.conn,'krivet-re-kr','KRIVET','https://www.krivet.re.kr')
        config=Path(__file__).resolve().parents[1]/'crawler/sites/configs/krivet-re-kr.json'
        manifest={'site_id':'krivet-re-kr','scope':'fixture source pages 1–3','observed_at':'2026-10-08T00:00:00Z',
                  'documents':[{'post_number':f'NH{i}','meta_url':f'https://www.krivet.re.kr/kor/sub.do?menuSn=23&pstNo=NH{i}'} for i in range(10)]}
        ids=[]
        for record in manifest['documents'][:8]:
            ids.append(self.backend.insert_document(self.conn,dict(record,site_id='krivet-re-kr',title='existing')))
        self.assertEqual(audit(self.conn,source_manifest=manifest)['source_comparison']['results']['missing_in_reviewed_source_scope'],2)
        crawler=GenericCrawler(config,self.conn,delay=0);crawler._config['detail_page']={}
        def fetch(url,params=None):
            start=(params['pageIndex']-1)*4
            html=''.join(f'<tr><td class="ta_l"><a href="{r["meta_url"]}">Source</a></td></tr>' for r in manifest['documents'][start:start+4])
            return BeautifulSoup('<table class="tb_base tb_list"><tbody>'+html+'</tbody></table>','html.parser')
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'sqlite'}),patch.object(crawler,'_fetch_page',side_effect=fetch),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(crawler.crawl(limit=3),2)
        self.assertEqual(audit(self.conn,source_manifest=manifest)['source_comparison']['results'],{'present':10})
        self.assertEqual([r[0] for r in self.conn.execute("SELECT seq_id FROM documents WHERE title='existing' ORDER BY seq_id")],ids)


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires disposable PostgreSQL')
class PostgresAuditTests(AuditContract,unittest.TestCase):
    def setUp(self):
        from crawler import db_pg
        self.backend=db_pg;self.conn=db_pg.open_db(os.environ['TEST_PG_DSN'])
        self.conn.execute('DROP SCHEMA public CASCADE');self.conn.execute('CREATE SCHEMA public');self.conn.commit()
        self.backend.init_db(self.conn)
    def tearDown(self):self.conn.close()

    def test_audit_transaction_is_read_only_and_repeatable(self):
        self.seed();observed=[]
        def inspect(event):
            observed.append((self.conn.execute('SHOW transaction_read_only').fetchone()['transaction_read_only'],
                             self.conn.execute('SHOW transaction_isolation').fetchone()['transaction_isolation']))
        audit(self.conn,emit=inspect)
        self.assertTrue(observed)
        self.assertEqual(set(observed),{('on','repeatable read')})
