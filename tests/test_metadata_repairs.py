import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
from crawler import db_libertree
from crawler.document_dates import normalize_date
from crawler.generic_crawler import GenericCrawler
from delivery.scripts.repair_metadata import make_plan, apply_plan, execute, rows

CONFIG = Path(__file__).resolve().parents[1] / 'crawler/sites/configs/krivet-re-kr.json'


class DateContractTests(unittest.TestCase):
    def test_named_month_retains_precision_without_inventing_day(self):
        for raw, status, precision in [('July 2025', 'partial', 'month'),
                                       (' FÉVRIER 2024 ', 'partial', 'month'),
                                       ('Jan. 0000', 'invalid', 'month'),
                                       ('July 2025 extra', 'unrecognized', None),
                                       ('Unknown 2025', 'unrecognized', None)]:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_date(raw), {'value': None, 'raw': raw, 'status': status, 'precision': precision})

    def test_formats_and_uncertainty(self):
        for raw, order, expected, status in [
            ('2026-08-07', None, '2026-08-07', 'normalized'),
            ('02 octobre 2026', None, '2026-10-02', 'normalized'),
            ('29 février 2024', None, '2024-02-29', 'normalized'),
            ('February 29, 2025', None, None, 'invalid'),
            ('2026년 8월 7일', None, '2026-08-07', 'normalized'),
            ('2026-08-07T23:10:00-03:00', None, '2026-08-07', 'normalized'),
            ('2026-08-07Tgarbage', None, None, 'unrecognized'),
            ('07.08.2026', None, None, 'ambiguous'),
            ('07.08.2026', 'DMY', '2026-08-07', 'normalized'),
            ('07.08.2026', 'MDY', '2026-07-08', 'normalized'),
            ('31.02.2026', None, None, 'invalid'),
            ('2026-02-29', None, None, 'invalid'),
            ('2026', None, None, 'partial'),
            ('2026-08', None, None, 'partial'),
            ('2026-13', None, None, 'invalid'),
            ('0000', None, None, 'invalid'),
            (None, None, None, 'missing'),
            ('', None, None, 'missing'),
        ]:
            with self.subTest(raw=raw, order=order):
                result = normalize_date(raw, order=order)
                self.assertEqual((result['value'], result['status']), (expected, status))
                self.assertEqual(result['raw'], raw)


class RepairContract:
    def seed(self, *, post='23', url=None, date='07.08.2026'):
        self.backend.upsert_site(self.conn, 'krivet-re-kr', 'KRIVET', 'https://www.krivet.re.kr')
        execute(self.conn, '''INSERT INTO documents(site_id,post_number,meta_url,title,published_date,listed_date)
                  VALUES(?,?,?,?,?,?)''', ('krivet-re-kr',post,url or self.url,'Fixture',date,'02 octobre 2026'))
        self.conn.commit()
        seq = rows(self.conn, 'SELECT max(seq_id) AS id FROM documents')[0]['id']
        self.conn.rollback()
        return seq

    def make(self, kind='krivet', **kw):
        result = make_plan(self.conn, site_id='krivet-re-kr',kind=kind, **kw)
        self.conn.rollback()
        return result

    def test_identity_repair_preserves_seq_and_prevents_duplicate(self):
        seq = self.seed()
        with self.assertRaisesRegex(ValueError, 'repair_required'):
            self.backend.insert_document(self.conn, {'site_id':'krivet-re-kr','post_number':'NH0000000791',
                                        'meta_url':self.url,'title':'New title'})
        self.conn.rollback()
        plan = self.make()
        self.assertEqual(apply_plan(self.conn, plan), 1)
        self.assertEqual(apply_plan(self.conn, plan), 0)
        self.assertEqual(self.make()['plan']['changes'], [])
        again = self.backend.insert_document(self.conn, {'site_id':'krivet-re-kr','post_number':'NH0000000791',
                       'meta_url':'https://www.krivet.re.kr/kor/sub.do?pstNo=NH0000000791&menuSn=99','title':'Again'})
        self.assertEqual(again,seq)
        self.conn.rollback()
        self.assertEqual(apply_plan(self.conn,plan,restore=True),1)
        self.assertEqual(rows(self.conn,'SELECT post_number FROM documents')[0]['post_number'],'23')

    def test_collision_and_new_collision_are_blocked(self):
        self.seed()
        plan = self.make()
        self.seed(post='NH0000000791',url=self.url+'&extra=1')
        with self.assertRaisesRegex(ValueError,'collision'):
            apply_plan(self.conn, plan)
        collision = self.make()
        self.assertTrue(collision['plan']['collisions'])
        with self.assertRaisesRegex(ValueError,'collisions'):
            apply_plan(self.conn,collision)
        self.assertEqual(rows(self.conn,'SELECT count(*) AS n FROM documents')[0]['n'],2)

    def test_date_repair_raw_preservation_restore_and_stale_preview(self):
        self.seed()
        plan = self.make('dates')
        self.assertEqual(apply_plan(self.conn,plan),1)
        row=rows(self.conn,'SELECT * FROM documents')[0]
        self.assertIsNone(row['published_date'])
        self.assertEqual(row['published_date_raw'],'07.08.2026')
        self.assertEqual(row['published_date_status'],'ambiguous')
        self.assertEqual(row['listed_date'],'2026-10-02')
        self.conn.rollback()
        self.assertEqual(self.make('dates')['plan']['changes'],[])
        resolved=self.make('dates',date_order='DMY',evidence='fixture declares day/month/year')
        self.assertEqual(apply_plan(self.conn,resolved),1)
        self.assertEqual(rows(self.conn,'SELECT published_date FROM documents')[0]['published_date'],'2026-08-07')
        self.conn.rollback()
        apply_plan(self.conn,resolved,restore=True)
        apply_plan(self.conn,plan,restore=True)
        self.assertEqual(rows(self.conn,'SELECT published_date FROM documents')[0]['published_date'],'07.08.2026')
        execute(self.conn,"UPDATE documents SET listed_date='changed'")
        self.conn.commit()
        with self.assertRaisesRegex(ValueError,'changed after preview'):
            apply_plan(self.conn,plan)

    def test_direct_insert_normalizes_both_dates(self):
        self.backend.upsert_site(self.conn,'fixture','fixture','https://fixture.invalid')
        self.backend.insert_document(self.conn, {'site_id':'fixture','meta_url':'https://fixture.invalid/1','title':'Date',
                  'published_date':'2026-08-07','listed_date':'02 octobre 2026'})
        row=rows(self.conn,'SELECT * FROM documents')[0]
        self.assertEqual(row['published_date'],'2026-08-07')
        self.assertEqual(row['listed_date'],'2026-10-02')
        self.assertEqual(row['listed_date_raw'],'02 octobre 2026')


class SQLiteRepairTests(RepairContract, unittest.TestCase):
    def setUp(self):
        self.backend=db_libertree
        self.conn=sqlite3.connect(':memory:')
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.backend.init_db(self.conn)
        self.url='https://www.krivet.re.kr/kor/sub.do?menuSn=23&pstNo=NH0000000791'

    def tearDown(self):
        self.conn.close()

    def test_legacy_schema_migration_preserves_values(self):
        self.seed()
        for name in ('published_date','listed_date'):
            for suffix in ('raw','status','precision'):
                self.conn.execute(f'ALTER TABLE documents DROP COLUMN {name}_{suffix}')
        self.conn.commit()
        self.backend.init_db(self.conn)
        self.backend.init_db(self.conn)
        row=rows(self.conn,'SELECT * FROM documents')[0]
        self.assertEqual(row['published_date'],'07.08.2026')
        self.assertEqual(row['published_date_status'],'legacy_unchecked')

    def test_real_generic_parser_repeat_three_and_cancel_resume(self):
        self.backend.upsert_site(self.conn,'krivet-re-kr','KRIVET','https://www.krivet.re.kr')
        def crawler():
            c=GenericCrawler(CONFIG,self.conn,delay=0)
            c._config['detail_page']={}
            return c
        def fetch(url,params=None):
            start=(params['pageIndex']-1)*4
            body=''.join(f'<tr><td></td><td>X</td><td class="ta_l"><a href="/kor/sub.do?menuSn=23&amp;pstNo=NH{i:010d}">Title {i}</a></td><td>2026-08-07</td></tr>' for i in range(start,min(start+4,10)))
            return BeautifulSoup(f'<table class="tb_base tb_list"><tbody>{body}</tbody></table>','html.parser')
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'sqlite'}), contextlib.redirect_stdout(io.StringIO()):
            for expected in (3,6,9):
                c=crawler()
                with patch.object(c,'_fetch_page',side_effect=fetch):
                    c.crawl(limit=3)
                self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0],expected)
            # Do not stop at two old pages before the tenth unseen document.
            c=crawler()
            with patch.object(c,'_fetch_page',side_effect=fetch):
                self.assertEqual(c.crawl(limit=3),1)
            self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0],10)
            c=crawler();c.delivery_mode='full'
            with patch.object(c,'_fetch_page',side_effect=fetch):
                self.assertEqual(c.crawl(limit=3),0)

    def test_invalid_id_is_failure_not_menu_fallback(self):
        c=GenericCrawler(CONFIG,self.conn,delay=0)
        for url in ('https://www.krivet.re.kr/kor/sub.do?menuSn=23',
                    self.url+'&pstNo=OTHER', self.url.replace('www.krivet.re.kr','example.com')):
            with self.assertRaises(ValueError):c._extract_id(url)
            self.assertEqual(c.delivery_source_failure,'invalid_document_id')
        self.assertEqual(c._extract_id(self.url),'NH0000000791')

    def test_cancel_after_save_then_resume_keeps_all_documents(self):
        from crawler.base_crawler import CrawlCancelled
        self.backend.upsert_site(self.conn,'krivet-re-kr','KRIVET','https://www.krivet.re.kr')
        html='<table class="tb_base tb_list"><tbody>'+''.join(
            f'<tr><td class="ta_l"><a href="/kor/sub.do?menuSn=23&amp;pstNo=NH{i}">Title</a></td></tr>'
            for i in range(5))+'</tbody></table>'
        def fetch(url,params=None):
            return BeautifulSoup(html if params['pageIndex']==1 else '', 'html.parser')
        def crawler():
            c=GenericCrawler(CONFIG,self.conn,delay=0);c._config['detail_page']={}
            return c
        c=crawler()
        c.delivery_should_cancel=lambda:self.conn.execute('SELECT count(*) FROM documents').fetchone()[0]>=2
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'sqlite'}),contextlib.redirect_stdout(io.StringIO()):
            with patch.object(c,'_fetch_page',side_effect=fetch),self.assertRaises(CrawlCancelled):c.crawl(limit=3)
            c=crawler()
            with patch.object(c,'_fetch_page',side_effect=fetch):self.assertEqual(c.crawl(limit=3),3)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0],5)


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires disposable PostgreSQL')
class PostgresRepairTests(RepairContract, unittest.TestCase):
    def setUp(self):
        from crawler import db_pg
        from delivery.be.migrate import migrate
        self.backend=db_pg
        self.conn=db_pg.open_db(os.environ['TEST_PG_DSN'])
        self.conn.execute('DROP SCHEMA public CASCADE')
        self.conn.execute('CREATE SCHEMA public')
        self.conn.commit()
        migrate(os.environ['TEST_PG_DSN'])
        self.url='https://www.krivet.re.kr/kor/sub.do?menuSn=23&pstNo=NH0000000791'

    def tearDown(self):self.conn.close()

    def test_catalogue_does_not_guess_dates(self):
        from delivery.be.catalogue import collect_catalogue, CatalogueFilters
        from datetime import date
        self.seed(date='07.08.2026')
        self.seed(post='NH2',url='https://fixture.invalid/2',date='2026-08-07')
        self.seed(post='NH3',url='https://fixture.invalid/3',date='2026-02-29')
        result=collect_catalogue(self.conn,CatalogueFilters(published_from=date(2026,1,1)),{})
        self.assertEqual(result['pagination']['total'],1)
        self.assertIn('unresolved_dates_excluded',result['query']['date_policy'])

    def test_id_repair_retains_file_and_translation_links(self):
        seq=self.seed()
        self.conn.execute('UPDATE documents SET pdf_downloaded=1,pdf_sha256=%s WHERE seq_id=%s',('b'*64,seq))
        self.conn.execute("""INSERT INTO document_translations(seq_id,source_field,target_locale,source_fingerprint,
          model_version,prompt_version,state,translation_text) VALUES(%s,'title','ko-KR',%s,'fixture','v1','completed','translation')""",(seq,'a'*64))
        self.conn.commit()
        plan=self.make()
        apply_plan(self.conn,plan)
        row=self.conn.execute('SELECT * FROM documents WHERE seq_id=%s',(seq,)).fetchone()
        self.assertEqual(row['pdf_sha256'],'b'*64)
        self.assertEqual(self.conn.execute('SELECT seq_id FROM document_translations').fetchone()['seq_id'],seq)

    def test_upgrade_legacy_pg_date_schema(self):
        seq=self.seed()
        for field in ('published_date','listed_date'):
            for suffix in ('raw','status','precision'):
                self.conn.execute(f'ALTER TABLE documents DROP COLUMN {field}_{suffix}')
        self.conn.commit()
        self.backend.init_db(self.conn)
        self.backend.init_db(self.conn)
        row=self.conn.execute('SELECT * FROM documents WHERE seq_id=%s',(seq,)).fetchone()
        self.assertEqual(row['published_date'],'07.08.2026')
        self.assertEqual(row['published_date_status'],'legacy_unchecked')

    def test_sqlite_transfer_retains_new_date_metadata(self):
        from delivery.scripts.migrate_sqlite_to_pg import load_tables, TABLES
        from psycopg import sql
        source=sqlite3.connect(':memory:')
        db_libertree.init_db(source)
        db_libertree.upsert_site(source,'transfer-fixture','Transfer','https://fixture.invalid')
        db_libertree.insert_document(source,{'site_id':'transfer-fixture','meta_url':'https://fixture.invalid/1',
                      'title':'Transfer','published_date':'07.08.2026','listed_date':'02 octobre 2026'})
        for name,columns,*_ in TABLES:
            if name not in ('sites','documents'):
                source.execute('CREATE TABLE '+name+' ('+','.join(c+' TEXT' for c in columns)+')')
        conn=self.conn
        class Copier:
            def copy_in(self,table,columns,records):
                with conn.cursor() as cursor:
                    statement=sql.SQL('COPY {} ({}) FROM STDIN').format(sql.Identifier(table),sql.SQL(',').join(map(sql.Identifier,columns)))
                    count=0
                    with cursor.copy(statement) as copy:
                        for row in records:copy.write_row(row);count+=1
                    return count
        with contextlib.redirect_stdout(io.StringIO()):load_tables(Copier(),source,None)
        row=self.conn.execute('SELECT * FROM documents').fetchone()
        self.assertIsNone(row['published_date'])
        self.assertEqual(row['published_date_raw'],'07.08.2026')
        self.assertEqual(row['published_date_status'],'ambiguous')
        self.assertEqual(row['listed_date'],'2026-10-02')
        source.close()
