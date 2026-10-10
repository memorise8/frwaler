"""Actual PDF parsing and DB persistence for repository items without abstracts."""
import hashlib,io,json,os,unittest
from unittest.mock import Mock,patch
import requests
from pypdf import PdfWriter
from crawler.base_crawler import CrawlCancelled
from tests.test_coupled_site_counts import CoupledContract
from tests.test_open_science_delivery_scopes import OpenScienceDeliveryScopes
from tests.test_onera_pdf_enrichment import pdf_bytes

class OpenSciencePdfCounts(CoupledContract,unittest.TestCase):
    SITE='open-science-canada-ca-communities';LIST_METHOD='_fetch_list';LIST_MODULE=False
    item=OpenScienceDeliveryScopes.item
    ORIGINAL=OpenScienceDeliveryScopes.ORIGINAL;ADDITIONAL=OpenScienceDeliveryScopes.ADDITIONAL
    def crawler(self):
        self.excluded={1,4}
        c=OpenScienceDeliveryScopes.crawler(self);original=self.fetch.side_effect
        def fetch(url,context='request'):
            result=original(url,context)
            if '/bundles' in url:
                uid=url.split('/items/')[1].split('/')[0];number=int(uid[:8]);body=pdf_bytes(number)
                entry=result['_embedded']['bundles'][0]['_embedded']['bitstreams']['_embedded']['bitstreams'][0]
                entry['_links']['content']['href']=c.base_url+'/server/api/core/bitstreams/'+uid+'/content'
                entry.update(sizeBytes=len(body),checkSum={'checkSumAlgorithm':'MD5','value':getattr(self,'expected_md5',hashlib.md5(body).hexdigest())})
                if getattr(self,'pdf_override',None) is not None:
                    entry['sizeBytes']=len(self.pdf_override);entry['checkSum']['value']=hashlib.md5(self.pdf_override).hexdigest()
            return result
        self.fetch.side_effect=fetch
        def get(url,**kw):
            number=int(url.split('/bitstreams/')[1][:8]);body=getattr(self,'pdf_override',pdf_bytes(number))
            response=requests.Response();response.url=url;response.status_code=getattr(self,'pdf_status',200)
            response.raw=io.BytesIO(body);response.headers['Content-Length']=str(getattr(self,'content_length',len(body)))
            if getattr(self,'redirect',None):response.status_code=302;response.headers['Location']=self.redirect
            return response
        c._session.get=self.pdf_get=Mock(side_effect=get);return c
    def test_pdf_excerpt_provenance_and_source_identity_without_false_file_flags(self):
        c=self.crawler();original=c._save_paper;papers={}
        def save(p):papers[p['external_id']]=dict(p);return original(p)
        c._save_paper=save;self.assertEqual(c.crawl(),8)
        for r in self.conn.execute('SELECT post_number,abstract,pdf_downloaded,text_extracted FROM documents'):
            row=r if isinstance(r,dict) else dict(zip(('post_number','abstract','pdf_downloaded','text_extracted'),r))
            number=int(row['post_number'][:8]);self.assertFalse(row['pdf_downloaded']);self.assertFalse(row['text_extracted'])
            if number in (1,4):
                self.assertIn('Original publisher aerospace',row['abstract']);meta=json.loads(papers[row['post_number']]['metadata'])
                self.assertEqual(meta['abstract_source'],'repository_pdf_text_excerpt');self.assertEqual(meta['abstract_pdf_sha256'],hashlib.sha256(pdf_bytes(number)).hexdigest())
        self.assertEqual(self.pdf_get.call_count,2)
    def test_checksum_mismatch_reports_incomplete_and_does_not_save_wrong_body(self):
        self.expected_md5='0'*32;c=self.crawler();self.assertEqual(c.crawl(),6);self.assertEqual(c.delivery_source_failure,'item_failed')
    def test_non_pdf_response_is_not_saved_as_report_body(self):
        self.pdf_override=b'<html>Not a PDF</html>';c=self.crawler();self.assertEqual(c.crawl(),6);self.assertEqual(c.delivery_source_failure,'item_failed')
    def test_failed_download_preserves_remaining_records_and_reports_incomplete(self):
        self.pdf_status=404;c=self.crawler();self.assertEqual(c.crawl(),6);self.assertEqual(c.delivery_source_failure,'item_failed')
    def test_image_pdf_is_an_explicit_incomplete_body_gap(self):
        writer=PdfWriter();writer.add_blank_page(width=300,height=200);buffer=io.BytesIO();writer.write(buffer);self.pdf_override=buffer.getvalue()
        c=self.crawler();self.assertEqual(c.crawl(),6);self.assertEqual(c.delivery_source_failure,'item_failed')
    def test_copy_restricted_pdf_stays_an_explicit_body_gap(self):
        from pypdf import PdfReader
        from pypdf.constants import UserAccessPermissions
        writer=PdfWriter();writer.append_pages_from_reader(PdfReader(io.BytesIO(pdf_bytes(1))))
        writer.encrypt(user_password='',owner_password='fixture-owner',permissions_flag=UserAccessPermissions.PRINT)
        buffer=io.BytesIO();writer.write(buffer);self.pdf_override=buffer.getvalue()
        c=self.crawler();self.assertEqual(c.crawl(),6);self.assertEqual(c.delivery_source_failure,'item_failed');self.assertFalse(c.delivery_exhausted)

    def test_access_denial_stops_before_later_items(self):
        self.pdf_status=403;c=self.crawler();self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'authentication_failed');self.assertEqual(self.pdf_get.call_count,1)
    def test_backfill_access_denial_keeps_first_unprocessed_position(self):
        self.pdf_status=403;c=self.crawler();c.delivery_mode='backfill'
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'authentication_failed')
        self.assertEqual(c.delivery_pending_cursor['offset'],0);self.assertEqual(self.pdf_get.call_count,1);self.assertFalse(c.delivery_exhausted)
    def test_backfill_pdf_recovery_then_resume_preserves_saved_rows(self):
        c=self.crawler();c.delivery_mode='backfill';self.assertEqual(c.crawl(limit=3),3)
        cursor=c.delivery_pending_cursor;before=self.identities();c=self.crawler();c.delivery_mode='backfill';c.delivery_cursor=cursor
        self.assertEqual(c.crawl(),5);self.assertTrue(c.delivery_exhausted)
        self.assertTrue(all(self.identities()[key]==value for key,value in before.items()));self.assertEqual(self.pdf_get.call_count,1)
    def test_off_repository_redirect_is_not_fetched(self):
        self.redirect='https://outside.invalid/report.pdf';c=self.crawler();self.assertEqual(c.crawl(),6)
        self.assertTrue(c.delivery_source_failure);self.assertFalse(any('outside.invalid' in call.args[0] for call in self.pdf_get.call_args_list))
    def test_size_limit_does_not_publish_partial_metadata(self):
        self.content_length=101*1024*1024;c=self.crawler();self.assertEqual(c.crawl(),6);self.assertEqual(c.delivery_source_failure,'item_failed')
    def test_text_stage_timeout_remains_incomplete(self):
        c=self.crawler()
        with patch('delivery.files.worker.run_stage',return_value={'success':False,'error':'stage_timeout'}):self.assertEqual(c.crawl(),6)
        self.assertEqual(c.delivery_source_failure,'item_failed')
    def test_cancellation_during_pdf_download_propagates(self):
        c=self.crawler();c.delivery_should_cancel=lambda:self.pdf_get.called
        with self.assertRaises(CrawlCancelled):c.crawl()
        self.assertEqual(self.count(),0)

@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class OpenSciencePdfPostgres(OpenSciencePdfCounts):
    BACKEND='postgres'
