"""Mixed-source traversal, durable exclusions, source/row drift and actual worker jobs."""
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from crawler.base_crawler import CrawlCancelled
from tests import test_academie_press_sections as fixtures
from tests.test_delivery_site_new_counts import InterruptedAfterCommit


class AcademieCursor(unittest.TestCase):
    BACKEND='sqlite'

    def setUp(self):
        self.case=fixtures.AcademieSections(methodName='test_all_three_sections_emitted_once_and_keep_existing_PDF_identity')
        self.case.BACKEND=self.BACKEND;self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.conn=self.case.conn;self.excluded=set();self.byte_suffix=b'';self.calls=[]

    def crawler(self,cursor=None):
        c=self.case.crawler();self.addCleanup(c._session.close)
        c.delivery_mode='backfill';c.delivery_cursor=cursor;self.calls=[]
        fetch=c._fetch_text
        def text(url,**kw):self.calls.append(url);return fetch(url,**kw)
        def pdf(url,**kw):self.calls.append(url);return b'%PDF-fixture-'+url.encode()+self.byte_suffix
        c._fetch_text=text;c._fetch_bytes=pdf
        c._extract_pdf_text=lambda raw,url:('LES FEMMES (ET LES FILLES) EN SCIENCES\n\fLes dernières parutions\n\f' if url in self.excluded else 'Scientific source report title\n'+'Original source research findings and explanations. '*15)
        return c

    def count(self):return self.case.case.count()
    def rows(self):return self.case.rows()
    def detail_calls(self):return [u for u in self.calls if not urlsplit(u).path.endswith('/espace-presse')]

    def test_partial_page_resume_and_terminal_recheck(self):
        cursor=None;all_details=[]
        for want in (2,2,2,0):
            c=self.crawler(cursor);self.assertEqual(c.crawl(limit=2),want)
            all_details.extend(self.detail_calls());cursor=c.delivery_pending_cursor
        self.assertEqual((len(all_details),len(set(all_details))),(6,6))
        self.assertEqual(self.count(),6);self.assertTrue(c.delivery_exhausted)
        self.assertEqual((cursor['page'],cursor['offset']),(1,1))

    def test_cancel_and_commit_before_checkpoint_preserve_all_existing_rows(self):
        c=self.crawler();c.delivery_should_cancel=lambda:self.count()>=3
        with self.assertRaises(CrawlCancelled):c.crawl()
        self.assertEqual(c.delivery_pending_cursor['offset'],3)
        c=self.crawler(c.delivery_pending_cursor)
        def committed(seq):
            if self.count()==4:raise InterruptedAfterCommit()
        c.delivery_document_saved=committed
        with self.assertRaises(InterruptedAfterCommit):c.crawl()
        self.assertEqual(c.delivery_pending_cursor['offset'],3);before=self.rows()
        c=self.crawler(c.delivery_pending_cursor);self.assertEqual(c.crawl(),2)
        self.assertEqual(self.rows()[:4],before);self.assertTrue(c.delivery_exhausted)

    def test_title_only_body_is_recorded_and_revalidated_before_remainder(self):
        self.excluded={fixtures.BASE+'/files/2.pdf'};c=self.crawler();self.assertEqual(c.crawl(limit=3),3)
        cursor=c.delivery_pending_cursor;self.assertEqual(cursor['offset'],4)
        self.assertEqual(set(json.loads(cursor['exclusions_json'])),{'files/2.pdf'})
        c=self.crawler(cursor);self.assertEqual(c.crawl(),2);self.assertTrue(c.delivery_exhausted)
        self.assertIn(fixtures.BASE+'/files/2.pdf',self.detail_calls());self.assertNotIn(fixtures.BASE+'/files/1.pdf',self.detail_calls())
        self.assertEqual(self.count(),5)

    def test_excluded_item_clears_previous_save_flags(self):
        self.excluded={fixtures.BASE+'/files/2.pdf'};c=self.crawler()
        items,_=c._parse_list_items(self.case.module._make_soup(fixtures.listing(0)),1,fixtures.START)
        self.assertTrue(c._process_item(items[0]));self.assertTrue(c._last_save_created)
        self.assertFalse(c._process_item(items[1]));self.assertFalse(c._last_save_created)
        self.assertIsNone(c._last_processed_seq_id);self.assertTrue(c._ac_last_exclusion)
        self.assertEqual(self.count(),1)

    def test_changed_excluded_bytes_or_newly_readable_body_invalidates_resume(self):
        self.excluded={fixtures.BASE+'/files/2.pdf'};c=self.crawler();c.crawl(limit=3);cursor=c.delivery_pending_cursor
        self.byte_suffix=b'changed source bytes';c=self.crawler(cursor);self.assertEqual(c.crawl(),0)
        self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertIsNone(c.delivery_pending_cursor)
        self.byte_suffix=b'';self.excluded.clear();c=self.crawler(cursor);self.assertEqual(c.crawl(),0)
        self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertEqual(self.count(),3)

    def test_changed_exclusion_fingerprint_and_extra_entry_are_rejected(self):
        self.excluded={fixtures.BASE+'/files/2.pdf'};c=self.crawler();c.crawl(limit=3);cursor=c.delivery_pending_cursor
        for exclusions in ({'files/2.pdf':'0'*64},{**json.loads(cursor['exclusions_json']),'files/3.pdf':'0'*64}):
            c=self.crawler(dict(cursor,exclusions_json=json.dumps(exclusions)))
            self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response')

    def test_identical_PDF_bytes_with_same_basename_keep_distinct_source_paths(self):
        c=self.crawler();fetch=c._fetch_text
        c._fetch_text=lambda url,**kw:fetch(url,**kw).replace('/files/2.pdf','/other/1.pdf')
        c._fetch_bytes=lambda *a,**kw:b'%PDF-identical-source-bytes'
        self.assertEqual(c.crawl(),6)
        ids={r['post_number']:r['seq_id'] for r in self.rows()}
        self.assertIn('files/1.pdf',ids);self.assertIn('other/1.pdf',ids)
        self.assertNotEqual(ids['files/1.pdf'],ids['other/1.pdf'])

    def test_source_card_change_and_stored_field_change_reject_resume(self):
        c=self.crawler();c.crawl(limit=5);cursor=c.delivery_pending_cursor
        c=self.crawler(cursor);fetch=c._fetch_text
        c._fetch_text=lambda url,**kw:fetch(url,**kw).replace('Report 1','Changed source title')
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response')
        self.conn.execute("UPDATE documents SET published_date_raw='Changed raw date' WHERE seq_id=1");self.conn.commit()
        c=self.crawler(cursor);self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response')

    def test_source_row_identity_change_or_deletion_never_recreates_on_resume(self):
        c=self.crawler();c.crawl(limit=2);cursor=c.delivery_pending_cursor
        self.conn.execute("UPDATE documents SET post_number='wrong legacy key' WHERE seq_id=1");self.conn.commit()
        c=self.crawler(cursor);self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response')
        c=self.crawler();self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'item_failed')
        self.conn.execute('DELETE FROM documents WHERE seq_id=1');self.conn.commit()
        c=self.crawler(cursor);self.assertEqual(c.crawl(),0);self.assertFalse(c.delivery_exhausted);self.assertEqual(self.count(),1)

    def test_file_and_translation_changes_do_not_change_source_anchor(self):
        c=self.crawler();c.crawl(limit=2);cursor=c.delivery_pending_cursor
        self.conn.execute("UPDATE documents SET pdf_downloaded=1,text_extracted=1,summary='Translated summary' WHERE seq_id=1");self.conn.commit()
        c=self.crawler(cursor);self.assertEqual(c.crawl(),4);self.assertTrue(c.delivery_exhausted)
        self.assertEqual(self.rows()[0]['summary'],'Translated summary')

    def test_existing_row_cannot_turn_into_an_exclusion(self):
        c=self.crawler();c.crawl(limit=2);before=self.rows();self.excluded={fixtures.BASE+'/files/1.pdf'}
        c=self.crawler();self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'item_failed')
        self.assertEqual(self.rows(),before);self.assertEqual(json.loads(c.delivery_pending_cursor['exclusions_json']),{})

    def test_transport_or_PDF_decoder_failure_is_not_a_body_exclusion(self):
        for failure in ('transport','nonPDF','decode'):
            c=self.crawler()
            if failure=='transport':c._fetch_bytes=lambda *a,**kw:None
            elif failure=='nonPDF':c._fetch_bytes=lambda *a,**kw:b'<html>Error</html>'
            else:c._extract_pdf_text=lambda *a:(_ for _ in ()).throw(ValueError('decoder failed'))
            self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'item_failed')
            self.assertEqual(json.loads(c.delivery_pending_cursor['exclusions_json']),{})
        c=self.crawler()
        with patch.object(self.case.module.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout=b'partial text',stderr=b'broken file')):
            with self.assertRaises(ValueError):self.case.module.AcademieSciencesFrEspacePresseCrawler._extract_pdf_text(c,b'%PDF-fixture','source')

    def test_http_failure_stops_at_exact_item_and_can_resume_after_repair(self):
        c=self.crawler();fetch=c._fetch_text;c._fetch_text=lambda url,**kw:None if url==fixtures.BASE+'/article' else fetch(url,**kw)
        self.assertEqual(c.crawl(),2);self.assertEqual(c.delivery_source_failure,'item_failed');self.assertEqual(c.delivery_pending_cursor['offset'],2)
        c=self.crawler(c.delivery_pending_cursor);self.assertEqual(c.crawl(),4);self.assertTrue(c.delivery_exhausted)

    def test_invalid_cursor_zero_limit_and_initial_cancel_perform_no_fetch(self):
        for cursor in ({'version':1,'page':False},{'version':2}):
            c=self.crawler(cursor);self.assertEqual(c.crawl(),0);self.assertEqual(self.calls,[])
        c=self.crawler();self.assertEqual(c.crawl(limit=0),0);self.assertEqual(self.calls,[])
        c=self.crawler();c.delivery_should_cancel=lambda:True
        with self.assertRaises(CrawlCancelled):c.crawl()
        self.assertEqual(self.calls,[])

    def test_budget_holds_without_false_exhaustion_and_keeps_progress(self):
        c=self.crawler();c.WALL_CLOCK_LIMIT=0;self.assertEqual(c.crawl(),0);self.assertEqual(self.calls,[])
        self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertFalse(c.delivery_exhausted)
        c=self.crawler();c.delivery_document_saved=lambda seq:setattr(c,'WALL_CLOCK_LIMIT',0)
        self.assertEqual(c.crawl(),1);self.assertEqual(c.delivery_pending_cursor['offset'],1);self.assertFalse(c.delivery_exhausted)
        c=self.crawler(c.delivery_pending_cursor);self.assertEqual(c.crawl(),5);self.assertTrue(c.delivery_exhausted)

    def test_missing_markup_promised_empty_page_and_safety_cap_hold(self):
        c=self.crawler();c._fetch_text=lambda *a,**kw:'<html><h1>Error</h1></html>'
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response')
        c=self.crawler();fetch=c._fetch_text;c._fetch_text=lambda u,**kw:fixtures.listing(2) if u.endswith('?page=1') else fetch(u,**kw)
        self.assertEqual(c.crawl(),5);self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertFalse(c.delivery_exhausted)
        c=self.crawler();c.MAX_PAGES=1;self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response')

    def test_full_and_incremental_ignore_injected_backfill_resume(self):
        c=self.crawler();c.crawl(limit=2);cursor=c.delivery_pending_cursor
        c=self.crawler(cursor);c.delivery_mode='incremental';self.assertEqual(c.crawl(),4)
        self.assertEqual(self.detail_calls()[0],fixtures.BASE+'/files/1.pdf');self.assertEqual(cursor['offset'],2)


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class AcademieCursorPostgres(AcademieCursor):
    BACKEND='postgres'

    def test_actual_worker_automatic_jobs_and_cancel_resume_reach_beyond_exclusion(self):
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        init_delivery_schema(self.conn);self.excluded={fixtures.BASE+'/files/2.pdf'}
        for index,(limit,cancel,want) in enumerate(((2,None,2),(2,1,1),(2,None,2),(2,None,0),(None,None,0))):
            c=self.crawler();before=self.count()
            if index in (0,2,4):jobs.enqueue_job(self.conn,c.site_id,mode='backfill',limit_n=limit)
            job=jobs.claim_next_job(self.conn);self.assertIsNotNone(job)
            def cancelled():
                if cancel and self.count()>=before+cancel:jobs.cancel_job(self.conn,job['id']);return True
                return False
            self.assertEqual(worker.run_job(self.conn,job,{c.site_id:lambda **kw:c},delay=0,should_cancel=cancelled),want)
            state=jobs.job_detail(self.conn,job['id'])['job'];self.assertEqual((state['saved_count'],state['status']),(want,'cancelled' if cancel else 'done'))
        p=self.conn.execute('SELECT * FROM crawl_site_progress').fetchone();self.assertEqual(p['items_done'],6);self.assertIsNotNone(p['completed_at'])
        self.assertEqual(set(json.loads(p['cursor']['exclusions_json'])),{'files/2.pdf'});self.assertEqual(self.count(),5)
        self.assertEqual(self.conn.execute("SELECT count(*) AS n FROM crawl_job_logs WHERE event='stalled'").fetchone()['n'],0)
        before=p['cursor'];c=self.crawler();jid=jobs.enqueue_job(self.conn,c.site_id,mode='incremental')
        self.assertEqual(worker.run_job(self.conn,jobs.claim_next_job(self.conn),{c.site_id:lambda **kw:c},delay=0),0)
        self.assertEqual(jobs.load_cursor(self.conn,c.site_id),before)
