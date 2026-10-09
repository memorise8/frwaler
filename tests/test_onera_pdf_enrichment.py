"""Native ONERA HTML + real PDF parsing and actual database persistence."""
import hashlib,io,json,os,unittest
from unittest.mock import Mock,patch
import requests
from crawler.base_crawler import CrawlCancelled
from tests.test_coupled_site_counts import CoupledContract
from tests import test_publication_press_contracts as press_contracts

def pdf_bytes(number):
    text=(f'Research report {number}. '+('Original publisher aerospace research evidence and results. '*5)).encode()
    stream=b'BT /F1 8 Tf 20 100 Td ('+text+b') Tj ET'
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream']
    pdf=b'%PDF-1.4\n';offsets=[]
    for n,obj in enumerate(objects,1):offsets.append(len(pdf));pdf+=str(n).encode()+b' 0 obj\n'+obj+b'\nendobj\n'
    start=len(pdf);pdf+=b'xref\n0 6\n0000000000 65535 f \n'+b''.join(f'{n:010d} 00000 n \n'.encode() for n in offsets)
    return pdf+b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n'+str(start).encode()+b'\n%%EOF\n'

class OneraPdfCounts(CoupledContract,unittest.TestCase):
    SITE='onera-fr-fr';LIST_METHOD='_fetch'
    first_list=press_contracts.OneraCounts.first_list;dates=press_contracts.OneraCounts.dates;is_list=press_contracts.OneraCounts.is_list
    def crawler(self):
        c=press_contracts.OneraCounts.crawler(self);original=self.fetch.side_effect
        def html(url,**kw):
            raw=original(url,**kw)
            if self.is_list(url):return raw
            number=int(url.rsplit('-',1)[1])
            if number in (1,4):
                raw=raw.replace('Aerospace research evidence. '*12,'')
                raw+=f'<div class="fichiers"><a href="/sites/default/files/press-{number}.pdf">Report {number}</a></div>'
            return raw
        self.fetch.side_effect=html
        def get(url,**kw):
            response=requests.Response();response.url=url;response.status_code=getattr(self,'pdf_status',200)
            number=int(url.rsplit('-',1)[1].split('.')[0]);body=getattr(self,'pdf_override',None)
            if body is None:body=pdf_bytes(number)
            response.raw=io.BytesIO(body);response.headers['Content-Length']=str(getattr(self,'content_length',len(body)))
            if getattr(self,'redirect',None):response.status_code=302;response.headers['Location']=self.redirect
            return response
        c._session.get=self.pdf_get=Mock(side_effect=get)
        return c

    def test_pdf_body_and_provenance_retained_without_false_file_flags(self):
        c=self.crawler();native=c._save_paper;papers={}
        def save(paper):papers[str(paper['external_id'])]=dict(paper);return native(paper)
        c._save_paper=save;self.assertEqual(c.crawl(),8)
        rows=self.conn.execute('SELECT post_number,abstract,pdf_downloaded,text_extracted FROM documents').fetchall()
        for r in rows:
            row=r if isinstance(r,dict) else dict(zip(['post_number','abstract','pdf_downloaded','text_extracted'],r))
            if row['post_number'] in ('1','4'):
                self.assertIn('Original publisher aerospace',row['abstract'])
                self.assertFalse(row['pdf_downloaded']);self.assertFalse(row['text_extracted'])
                meta=json.loads(papers[row['post_number']]['metadata'])
                self.assertEqual(meta['abstract_source'],'publisher_pdf_text_excerpt')
                self.assertEqual(meta['abstract_pdf_sha256'],hashlib.sha256(pdf_bytes(int(row['post_number']))).hexdigest())
        self.assertEqual(self.pdf_get.call_count,2)

    def test_non_pdf_response_reports_incomplete(self):
        self.pdf_override=b'<html>Not a PDF</html>'
        c=self.crawler();self.assertEqual(c.crawl(),6);self.assertEqual(c.delivery_source_failure,'item_failed')

    def test_failed_pdf_is_not_silently_excluded(self):
        self.pdf_status=500
        c=self.crawler();self.assertEqual(c.crawl(),6);self.assertEqual(c.delivery_source_failure,'item_failed')

    def test_access_denial_stops_before_other_documents(self):
        self.pdf_status=403
        c=self.crawler();self.assertEqual(c.crawl(),0)
        self.assertEqual(c.delivery_source_failure,'authentication_failed');self.assertEqual(self.pdf_get.call_count,1)

    def test_redirect_outside_reviewed_publisher_is_not_fetched(self):
        self.redirect='https://other.invalid/file.pdf'
        c=self.crawler();self.assertEqual(c.crawl(),6)
        self.assertFalse(any('other.invalid' in call.args[0] for call in self.pdf_get.call_args_list))
        self.assertTrue(c.delivery_source_failure)

    def test_pdf_size_limit_reports_incomplete(self):
        self.content_length=11*1024*1024
        c=self.crawler();self.assertEqual(c.crawl(),6);self.assertTrue(c.delivery_source_failure)

    def test_extraction_timeout_does_not_save_a_pdf_only_record(self):
        c=self.crawler()
        with patch('delivery.files.worker.run_stage',return_value={'success':False,'error':'stage_timeout'}):
            self.assertEqual(c.crawl(),6)
        self.assertEqual(c.delivery_source_failure,'item_failed')

    def test_cancellation_during_pdf_stream_is_not_swallowed(self):
        c=self.crawler();c.delivery_should_cancel=lambda:self.pdf_get.called
        with self.assertRaises(CrawlCancelled):c.crawl()
        self.assertEqual(self.count(),0)

@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class OneraPdfPostgres(OneraPdfCounts):
    BACKEND='postgres'
