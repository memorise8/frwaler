"""Publisher TEXT fallback keeps PDF extraction permissions and original metadata."""
import copy,hashlib,io,json,os,unittest
from unittest.mock import Mock
import requests
from pypdf import PdfReader,PdfWriter
from pypdf.constants import UserAccessPermissions
from crawler.base_crawler import CrawlCancelled
from tests.test_coupled_site_counts import CoupledContract
from tests.test_open_science_pdf_enrichment import OpenSciencePdfCounts
from tests.test_open_science_delivery_scopes import OpenScienceDeliveryScopes
from tests.test_onera_pdf_enrichment import pdf_bytes

class PublishedText(CoupledContract,unittest.TestCase):
    SITE='open-science-canada-ca-communities';LIST_METHOD='_fetch_list';LIST_MODULE=False
    item=OpenScienceDeliveryScopes.item
    ORIGINAL=OpenScienceDeliveryScopes.ORIGINAL;ADDITIONAL=OpenScienceDeliveryScopes.ADDITIONAL
    def crawler(self):
        if getattr(self,'restricted',True):
            w=PdfWriter();w.append_pages_from_reader(PdfReader(io.BytesIO(pdf_bytes(1))));w.encrypt(user_password='',owner_password='fixture-owner',permissions_flag=UserAccessPermissions.PRINT);b=io.BytesIO();w.write(b);self.pdf_override=b.getvalue()
        c=OpenSciencePdfCounts.crawler(self);original=self.fetch.side_effect;pdf_get=c._session.get;self.text_bytes=getattr(self,'payload',b'Publisher-deposited report text on scientific methods and independently recorded measurements. '*40);self.text_requests=[]
        def fetch(url,context='request'):
            result=original(url,context)
            if '/bundles?' in url:
                number=int(url.split('/items/')[1][:8]);uid=f'{1000+number:08d}-0000-0000-0000-000000000000';href=c.base_url+'/server/api/core/bundles/'+uid+'/bitstreams';bit=dict(uuid=uid,type='bitstream',bundleName='TEXT',name=f'report-{number}.pdf.txt',sizeBytes=len(self.text_bytes),checkSum=dict(checkSumAlgorithm='MD5',value=hashlib.md5(self.text_bytes).hexdigest()),_links=dict(content=dict(href=c.base_url+'/server/api/core/bitstreams/'+uid+'/content')));tree=dict(_embedded=dict(bitstreams=[bit]),page=dict(number=0,size=20,totalPages=1,totalElements=1),_links=dict(self=dict(href=href)));bundle=dict(uuid=uid,type='bundle',name='TEXT',_links=dict(bitstreams=dict(href=href)),_embedded=dict(bitstreams=tree))
                getattr(self,'mutate',lambda b:None)(bundle);result['_embedded']['bundles'].append(bundle);result['page']['totalElements']=2
            return result
        def get(url,**kw):
            if int(url.split('/bitstreams/')[1][:8])<1000:return pdf_get(url,**kw)
            self.text_requests.append((url,kw));r=requests.Response();r.url=url;r.status_code=getattr(self,'text_status',200);r.raw=io.BytesIO(getattr(self,'download_bytes',self.text_bytes));r.headers['Content-Length']=str(getattr(self,'response_length',len(self.text_bytes)));return r
        self.fetch.side_effect=fetch;c._session.get=Mock(side_effect=get);return c
    def first_body(self):
        r=self.conn.execute('SELECT abstract,pdf_downloaded,text_extracted FROM documents ORDER BY seq_id LIMIT 1').fetchone();return tuple(r.values()) if isinstance(r,dict) else tuple(r)
    def test_published_TEXT_recovers_restricted_PDF_and_keeps_original_file_refs(self):
        c=self.crawler();papers=[];original=c._save_paper
        def save(p):papers.append(p);return original(p)
        c._save_paper=save;self.assertEqual(c.crawl(),8);self.assertTrue(c.delivery_exhausted);self.assertEqual(len(self.text_requests),2)
        meta=json.loads(papers[0]['metadata']);self.assertEqual(meta['abstract_source'],'repository_published_text_excerpt');self.assertEqual(meta['abstract_pdf_text_failure'],'pdf_copy_restricted');self.assertEqual(meta['abstract_repository_text_sha256'],hashlib.sha256(self.text_bytes).hexdigest());self.assertEqual(len(meta['bitstreams']),1);self.assertTrue(meta['bitstreams'][0]['name'].endswith('.pdf'));self.assertEqual(self.first_body(),(' '.join(self.text_bytes.decode()[:12000].split())[:3000],0,0));self.assertTrue(all(kw['allow_redirects'] is False for _,kw in self.text_requests))
    def test_successful_PDF_text_does_not_fetch_repository_TEXT(self):
        self.restricted=False;c=self.crawler();self.assertEqual(c.crawl(),8);self.assertEqual(self.text_requests,[])
    def test_counterpart_filename_and_parent_bundle_contract_must_match(self):
        changes=[lambda b:b['_embedded']['bitstreams']['_embedded']['bitstreams'][0].update(name='other.pdf.txt'),lambda b:b['_embedded']['bitstreams']['page'].update(totalElements=2),lambda b:b['_links']['bitstreams'].update(href='https://outside.invalid/text')]
        for change in changes:
            self.mutate=change;c=self.crawler();c.delivery_mode='backfill';self.assertEqual(c.crawl(limit=1),0);self.assertFalse(c.delivery_exhausted);self.assertEqual(c.delivery_pending_cursor['offset'],0);self.assertFalse(self.text_requests)
    def test_ambiguous_TEXT_counterparts_are_not_guessed(self):
        def duplicate(b):
            tree=b['_embedded']['bitstreams'];tree['_embedded']['bitstreams'].append(copy.deepcopy(tree['_embedded']['bitstreams'][0]));tree['page']['totalElements']=2
        self.mutate=duplicate;c=self.crawler();c.delivery_mode='backfill';self.assertEqual(c.crawl(limit=1),0);self.assertEqual(self.text_requests,[])
    def test_foreign_content_destination_is_rejected_before_fetch(self):
        self.mutate=lambda b:b['_embedded']['bitstreams']['_embedded']['bitstreams'][0]['_links']['content'].update(href='https://outside.invalid/text');c=self.crawler();c.delivery_mode='backfill';self.assertEqual(c.crawl(limit=1),0);self.assertEqual(self.text_requests,[])
    def test_wrong_checksum_or_incomplete_text_does_not_save(self):
        self.download_bytes=b'X'*len(b'Publisher-deposited report text on scientific methods and independently recorded measurements. '*40);c=self.crawler();c.delivery_mode='backfill';self.assertEqual(c.crawl(limit=1),0);self.assertFalse(c.delivery_exhausted)
    def test_HTML_with_a_matching_checksum_is_not_a_body(self):
        self.payload=b'<html>'+b'Error response. '*50+b'</html>';c=self.crawler();c.delivery_mode='backfill';self.assertEqual(c.crawl(limit=1),0)
    def test_TEXT_size_budget_and_redirect_are_rejected(self):
        self.response_length=11*1024**2;c=self.crawler();c.delivery_mode='backfill';self.assertEqual(c.crawl(limit=1),0)
        self.response_length=len(self.text_bytes);self.text_status=302;c=self.crawler();c.delivery_mode='backfill';self.assertEqual(c.crawl(limit=1),0)
    def test_TEXT_access_failure_holds_current_cursor(self):
        self.text_status=403;c=self.crawler();c.delivery_mode='backfill';self.assertEqual(c.crawl(limit=1),0);self.assertEqual(c.delivery_source_failure,'authentication_failed');self.assertEqual(c.delivery_pending_cursor['offset'],0);self.assertEqual(len(self.text_requests),1)
    def test_cancellation_during_TEXT_read_is_not_a_failed_or_saved_item(self):
        c=self.crawler();c.delivery_mode='backfill';c.delivery_should_cancel=lambda:bool(self.text_requests)
        with self.assertRaises(CrawlCancelled):c.crawl(limit=1)
        self.assertEqual(self.count(),0);self.assertEqual(c.delivery_pending_cursor['offset'],0)
    def test_cursor_resume_preserves_rows_after_TEXT_recovery(self):
        c=self.crawler();c.delivery_mode='backfill';self.assertEqual(c.crawl(limit=3),3);before=self.identities();cursor=c.delivery_pending_cursor;c=self.crawler();c.delivery_mode='backfill';c.delivery_cursor=cursor;self.assertEqual(c.crawl(),5);self.assertTrue(c.delivery_exhausted);self.assertTrue(all(self.identities()[k]==v for k,v in before.items()))

@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class PublishedTextPostgres(PublishedText):BACKEND='postgres'
