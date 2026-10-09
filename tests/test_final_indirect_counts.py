"""Final three indirect-counter native source paths with real persistence."""
import json,os,re,time,unittest
from unittest.mock import Mock,patch
from urllib.parse import parse_qs,urlsplit
from crawler.base_crawler import CrawlCancelled
from tests.test_coupled_site_counts import CoupledContract

DATES=['2025','July 2025','2025-02-30','2025-07-11T12:34:56Z','2025-07-11 suffix','03/04/2025','July 11, 2025','2025-07']
class FinalIndirectContract(CoupledContract):
    def body(self,n):return '' if n in getattr(self,'excluded',set()) else 'Original scientific and government publication text. '*10
    def date(self,n):return getattr(self,'dates',['2025-07-11']*8)[n-1]
    def test_cancel_after_final_record_is_observed(self):
        c=self.crawler();c.delivery_should_cancel=lambda:self.count()>=8
        with self.assertRaises(CrawlCancelled):c.crawl()
        self.assertEqual(self.count(),8)
    def test_zero_limit_performs_no_fetch(self):
        c=self.crawler();self.assertEqual(c.crawl(limit=0),0);self.fetch.assert_not_called()
    def test_excluded_first_page_does_not_hide_later_records(self):
        self.excluded=set(range(1,6));self.assertEqual(self.crawler().crawl(),3);self.assertEqual(self.count(),3)
    def test_failed_entry_is_reported(self):
        c=self.crawler();self.fetch.side_effect=lambda *a,**kw:None
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'fetch_failed')
    def test_raw_source_dates_keep_precision(self):
        self.dates=DATES;self.assertEqual(self.crawler().crawl(),8)
        rows=self.conn.execute('SELECT published_date_raw,published_date_status FROM documents ORDER BY seq_id').fetchall()
        actual=[tuple(r.values()) if isinstance(r,dict) else tuple(r) for r in rows]
        self.assertEqual(actual,list(zip(DATES,['partial','partial','invalid','normalized','unrecognized','ambiguous','normalized','partial'])))
    def test_repeated_source_page_reports_incomplete(self):
        c=self.crawler();original=self.fetch.side_effect;first={}
        def fetch(url,*args,**kw):
            if '/bundles' in url or 'nrc-publications.canada.ca' in url:return original(url,*args,**kw)
            if not first:first.update(url=url,args=args,kw=kw)
            return original(first['url'],*first['args'],**first['kw'])
        self.fetch.side_effect=fetch;self.assertEqual(c.crawl(),5);self.assertEqual(c.delivery_source_failure,'invalid_response')

class NemertesCounts(FinalIndirectContract,unittest.TestCase):
    SITE='nemertes-library-upatras-gr-browse';LIST_METHOD='_fetch_json';LIST_MODULE=False
    def crawler(self):
        c=self.configured(self.module.NemertesLibraryUpatrasGrBrowseCrawler(self.conn))
        def fetch(url,params=None):
            if '/bundles' in url:
                return {'_embedded':{'bundles':[{'name':'ORIGINAL','_embedded':{'bitstreams':{'_embedded':{'bitstreams':[{'name':'readme.txt','_links':{'content':{'href':'https://fixture.invalid/readme.txt'}}},{'name':'report.pdf','_links':{'content':{'href':'https://fixture.invalid/report.pdf'}}}]}}}}]}}
            page=params['page'];items=[]
            for n in {0:range(1,6),1:range(6,9)}.get(page,[]):
                items.append({'uuid':f'{n:08d}-0000-0000-0000-000000000000','handle':f'123456789/{n}','metadata':{'dc.title':[{'value':f'Research {n}'}],'dc.description.abstract':[{'value':self.body(n)}],'dc.date.issued':[{'value':self.date(n)}],'dc.date.accessioned':[{'value':self.date(n)}]}})
            return {'_embedded':{'items':items},'page':{'totalPages':2}}
        c._fetch_json=Mock(side_effect=fetch);self.fetch=c._fetch_json;return c
    def test_original_bundle_prefers_pdf_over_readme(self):
        self.assertEqual(self.crawler().crawl(),8)
        for r in self.conn.execute('SELECT pdf_url FROM documents'):self.assertEqual(r['pdf_url'] if isinstance(r,dict) else r[0],'https://fixture.invalid/report.pdf')
    def test_bundle_failure_does_not_persist_permanently_missing_pdf(self):
        c=self.crawler();original=self.fetch.side_effect;self.fetch.side_effect=lambda url,params=None:None if '/bundles' in url else original(url,params)
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'item_failed')
    def test_error_object_is_not_empty_collection(self):
        c=self.crawler();self.fetch.side_effect=lambda *a,**kw:{'error':'unavailable'}
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response')

class ScienceCanadaCounts(FinalIndirectContract,unittest.TestCase):
    SITE='science-libraries-canada-ca-eng';LIST_METHOD='_fetch';LIST_MODULE=True
    def crawler(self):
        c=self.configured(self.module.ScienceLibrariesCanadaCaEngCrawler(self.conn))
        def fetch(url):
            if 'nrc-publications.canada.ca' in url:
                n=int(parse_qs(urlsplit(url).query)['id'][0][:8])
                fields={'Abstract':self.body(n),'NPARC number':str(n),'Date published':self.date(n),'Record created':self.date(n),'Download':'<a href="/eng/view/fulltext/?id=report.pdf">PDF</a>'}
                return f'<h1><span class="citation_title">Report {n}</span></h1><table class="table-viewobject">'+''.join(f'<tr><th>{k}</th><td>{v}</td></tr>' for k,v in fields.items())+'</table>'
            page=int(parse_qs(urlsplit(url).query)['pg'][0]);ids={1:range(1,6),2:range(6,9)}.get(page,[])
            return '<ol class="search-results">'+''.join(f'<li><article><div class="metadata-title"><a href="https://nrc-publications.canada.ca/eng/view/object/?id={n:08d}-0000-0000-0000-000000000000">Report {n}</a></div></article></li>' for n in ids)+'</ol>'
        self.fetch=self.enterContext(patch.object(self.module,'_fetch',side_effect=fetch));self.enterContext(patch.object(self.module,'_fetch_head',return_value='Content-Disposition: attachment; filename="report.pdf"'));return c
    def test_relative_download_belongs_to_detail_host(self):
        self.assertEqual(self.crawler().crawl(),8)
        for r in self.conn.execute('SELECT pdf_url FROM documents'):self.assertEqual(r['pdf_url'] if isinstance(r,dict) else r[0],'https://nrc-publications.canada.ca/eng/view/fulltext/?id=report.pdf')

class TransportNZCounts(FinalIndirectContract,unittest.TestCase):
    SITE='transport-govt-nz-documents-and-public';LIST_METHOD='_curl_text';LIST_MODULE=False
    def listing(self,ids):
        return ''.join(f'<div class="listing-item"><div id="publication-{n}"></div><h3 class="listing-title">Report {n}</h3><div class="listing-description">{self.body(n)}</div><span class="datestamp-month">{self.date(n)}</span><a href="/download/{n}">Download PDF</a></div>' for n in ids)
    def crawler(self):
        c=self.configured(self.module.TransportGovtNZDocumentsAndPublicCrawler(self.conn));c._PAGE_SIZE=5
        def fetch(url,**kw):
            if '/cdx/' in url:return '[]'
            if 'web.archive.org' in url:return self.listing(range(1,9))
            page=int(parse_qs(urlsplit(url).query).get('start',['0'])[0])//5
            return json.dumps({'ResultsList':self.listing({0:range(1,6),1:range(6,9)}.get(page,[])),'loadmore':page==0})
        c._curl_text=Mock(side_effect=fetch);self.fetch=c._curl_text;c._curl_head=Mock(return_value='HTTP/1.1 302 Found\r\nLocation: https://www.transport.govt.nz/assets/report.pdf\r\n');c._pdf_text=Mock(return_value=None);return c
    def test_known_live_records_do_not_trigger_archive_fallback(self):
        self.assertEqual(self.crawler().crawl(),8);c=self.crawler()
        with patch.object(c,'_crawl_archive_fallback') as archive:self.assertEqual(c.crawl(),0);archive.assert_not_called()
    def test_live_access_denial_does_not_trigger_archive_fallback(self):
        c=self.crawler();self.fetch.side_effect=lambda *a,**kw:'Incapsula incident ID _Incapsula_Resource'
        with patch.object(c,'_crawl_archive_fallback') as archive:self.assertEqual(c.crawl(),0);archive.assert_not_called()
        self.assertTrue(c.delivery_source_failure)
    def test_archive_native_path_counts_only_new_rows(self):
        for expected in (3,3,2,0):
            c=self.crawler();self.assertEqual(c._crawl_archive_fallback(3,set(),0,'3',time.time()),expected)
        self.assertEqual(self.count(),8)
    def test_report_coverage_year_does_not_become_publication_january_first(self):
        c=self.crawler();self.assertIsNone(c._date_from_title('Strategic Intentions 2024–2028'))
    def test_arbitrary_body_date_does_not_become_publication_date(self):
        c=self.crawler();text='During 01 July 2010 a prior policy was introduced. '+self.body(1)
        body,date=c._abstract_from_pdf_text(text);self.assertTrue(body);self.assertIsNone(date)
    def test_error_object_does_not_trigger_archive_or_look_like_empty_source(self):
        c=self.crawler();self.fetch.side_effect=lambda *a,**kw:'{"error":"unavailable"}'
        with patch.object(c,'_crawl_archive_fallback') as archive:self.assertEqual(c.crawl(),0);archive.assert_not_called()
        self.assertEqual(c.delivery_source_failure,'invalid_response')
    def test_valid_empty_live_collection_does_not_trigger_archive(self):
        c=self.crawler();self.fetch.side_effect=lambda *a,**kw:'{"ResultsList":"","loadmore":false}'
        with patch.object(c,'_crawl_archive_fallback') as archive:self.assertEqual(c.crawl(),0);archive.assert_not_called()
        self.assertFalse(c.delivery_source_failure)

for cls in (NemertesCounts,ScienceCanadaCounts,TransportNZCounts):
    pg=type(cls.__name__+'Postgres',(cls,),{'BACKEND':'postgres'})
    globals()[pg.__name__]=unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')(pg)
del cls,pg

@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class FinalIndirectWorkerLifecycle(unittest.TestCase):
    def test_actual_worker_counts_cancelled_rows_and_preserves_source_failures(self):
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        for cls in (NemertesCounts,ScienceCanadaCounts,TransportNZCounts):
            with self.subTest(site=cls.SITE):
                case=cls(methodName='test_repeat_three_reaches_remainder_and_then_zero');case.BACKEND='postgres'
                try:
                    case.setUp();init_delivery_schema(case.conn)
                    for limit,cancel,expected in [(3,None,3),(3,None,3),(None,2,2),(None,None,0)]:
                        c=case.crawler();before=case.count();jid=jobs.enqueue_job(case.conn,case.SITE,mode='full',limit_n=limit)
                        job=jobs.claim_next_job(case.conn)
                        def should_cancel():
                            if cancel and case.count()>=before+cancel:jobs.cancel_job(case.conn,jid);return True
                            return False
                        returned=worker.run_job(case.conn,job,{case.SITE:lambda **kw:c},delay=0,should_cancel=should_cancel)
                        detail=jobs.job_detail(case.conn,jid)['job']
                        self.assertEqual((returned,detail['saved_count'],detail['status']),(expected,expected,'cancelled' if cancel else 'done'))
                        self.assertEqual(case.count()-before,expected)
                    c=case.crawler();case.fetch.side_effect=lambda *a,**kw:None
                    jid=jobs.enqueue_job(case.conn,case.SITE,mode='full')
                    self.assertEqual(worker.run_job(case.conn,jobs.claim_next_job(case.conn),{case.SITE:lambda **kw:c},delay=0),0)
                    detail=jobs.job_detail(case.conn,jid)['job'];self.assertEqual(detail['status'],'failed');self.assertIn('fetch_failed',detail['error']);self.assertEqual(case.count(),8)
                finally:case.doCleanups()
