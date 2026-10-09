"""Five native Greek press parsers with real SQLite/PostgreSQL persistence."""
import json,os,re,unittest
from unittest.mock import Mock,patch
from urllib.parse import parse_qs,urlsplit
from tests.test_coupled_site_counts import CoupledContract

class GreekPressContract(CoupledContract):
    LIST_METHOD='_curl_get'
    def crawler(self):
        c=self.configured(getattr(self.module,self.CLASS)(self.conn))
        self.first_list=self.module._LIST_URL if self.LIST_MODULE else c.START_URL if hasattr(c,'START_URL') else c._API_BASE
        if self.SITE=='mintour-gov-gr-deltia-typoy':self.first_list=c.LIST_URL
        def fetch(url,**kw):
            match=re.search(r'/(?:report-(\d+)|(\d+)-report)',url)
            if not match:
                query=parse_qs(urlsplit(url).query)
                page=int(query.get('page',query.get('paged',['1']))[0])
                if '/page/' in url:page=int(url.rstrip('/').rsplit('/',1)[1])
                if 'start' in query:page=int(query['start'][0])//self.module._PAGE_SIZE+1
                ids={1:range(1,6),2:range(6,9)}.get(page,[])
                if self.SITE=='ypes-gr-category':
                    return json.dumps([{'id':n,'slug':f'report-{n}','link':c.base_url+f'/report-{n}/','title':{'rendered':f'Report {n}'},'date':'2025-07-11T12:00:00Z','content':{'rendered':self.abstract(n)}} for n in ids]).encode()
                cards=[]
                for n in ids:
                    target=c.base_url+(f'/{n}-report' if self.LIST_MODULE else f'/report-{n}/')
                    title=f'11-07-25 Research {n}' if self.LIST_MODULE else f'Report {n}'
                    cards.append(f'<article class="elementor-post custom-article-item post-{n}"><div id="post-{n}"></div><h4 class="entry-title elementor-post__title custom-article-title"><a href="{target}">{title}</a></h4><span class="elementor-post-date posted-on">11/07/2025</span></article>')
                next_link='<div class="pagenavi"><a class="page-numbers" href="?paged=2">2</a></div>' if page==1 else ''
                return ('<div class="post-list custom-article-list">'+''.join(cards)+'</div>'+next_link).encode()
            n=int(match[1] or match[2]);title=f'11-07-25 Research {n}' if self.LIST_MODULE else f'Report {n}'
            return (f'<meta property="og:title" content="{title}"><meta property="article:published_time" content="2025-07-11T12:00:00Z"><link rel="shortlink" href="{c.base_url}/?p={n}"><article id="post-{n}"><h1 class="entry-title">{title}</h1><div class="entry-content elementor-widget-theme-post-content item-pageDeltia-Typou"><p>'+self.abstract(n)+'</p></div></article>').encode()
        self.fetch=Mock(side_effect=fetch)
        if self.LIST_MODULE:self.enterContext(patch.object(self.module,'_curl_get',self.fetch))
        else:c._curl_get=self.fetch
        return c
    def abstract(self,n):return '' if n in getattr(self,'excluded',set()) else 'Public government research and press evidence. '*10
    def test_cancel_after_final_document_is_observed(self):
        from crawler.base_crawler import CrawlCancelled
        c=self.crawler();c.delivery_should_cancel=lambda:self.count()>=8
        with self.assertRaises(CrawlCancelled):c.crawl()
        self.assertEqual(self.count(),8)
    def test_zero_limit_performs_no_fetch(self):
        c=self.crawler();self.assertEqual(c.crawl(limit=0),0);self.fetch.assert_not_called()
    def test_fetch_failure_is_incomplete(self):
        c=self.crawler();self.fetch.side_effect=lambda *a,**k:None
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'fetch_failed')
    def test_repeated_source_page_reports_incomplete(self):
        c=self.crawler();original=self.fetch.side_effect
        def fetch(url,**kw):return original(self.first_list,**kw) if not re.search(r'/(?:report-\d+|\d+-report)',url) else original(url,**kw)
        self.fetch.side_effect=fetch
        self.assertEqual(c.crawl(),5);self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertLessEqual(self.fetch.call_count,8)
    def test_excluded_first_page_does_not_hide_later_records(self):
        self.excluded=set(range(1,6));self.assertEqual(self.crawler().crawl(),3);self.assertEqual(self.count(),3)

class HtmlGreekPressContract(GreekPressContract):
    def test_failed_details_are_not_successful_empty_collection(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda url,**kw:None if re.search(r'/(?:report-\d+|\d+-report)',url) else original(url,**kw)
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'item_failed')

class YpesCounts(GreekPressContract,unittest.TestCase):
    SITE='ypes-gr-category';CLASS='YpesGrCategoryCrawler';LIST_MODULE=False
    def test_error_object_is_not_valid_empty_api_response(self):
        c=self.crawler();self.fetch.side_effect=lambda *a,**k:b'{"code":"rest_forbidden","message":"Access denied"}'
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response')
    def test_valid_empty_array_is_distinct_from_error_object(self):
        c=self.crawler();self.fetch.side_effect=lambda *a,**k:b'[]'
        self.assertEqual(c.crawl(),0);self.assertFalse(c.delivery_source_failure)
    def test_missing_native_id_is_not_persisted_as_none(self):
        c=self.crawler();original=self.fetch.side_effect
        def fetch(url,**kw):
            rows=json.loads(original(url,**kw))
            if rows:rows[0]['id']=None
            return json.dumps(rows).encode()
        self.fetch.side_effect=fetch;self.assertEqual(c.crawl(),6)
        self.assertNotIn('None',self.identities());self.assertTrue(c.delivery_source_failure)
    def test_entire_source_date_is_retained(self):
        c=self.crawler();original=self.fetch.side_effect
        dates=['2025','July 2025','2025-02-30','2025-07-11T12:34:56Z','2025-07-11 suffix','03/04/2025','July 11, 2025','2025-07']
        def fetch(url,**kw):
            rows=json.loads(original(url,**kw))
            for r in rows:r['date']=dates[r['id']-1]
            return json.dumps(rows).encode()
        self.fetch.side_effect=fetch;self.assertEqual(c.crawl(),8)
        rows=self.conn.execute('SELECT published_date_raw,published_date_status FROM documents ORDER BY seq_id').fetchall()
        actual=[tuple(r.values()) if isinstance(r,dict) else tuple(r) for r in rows]
        self.assertEqual(actual,list(zip(dates,['partial','partial','invalid','normalized','unrecognized','ambiguous','normalized','partial'])))

class MinscfaCounts(HtmlGreekPressContract,unittest.TestCase):
    SITE='minscfa-gov-gr-grafeio-typou';CLASS='MinscfaGovGrGrafeioTypouCrawler';LIST_MODULE=False
class JusticeCounts(HtmlGreekPressContract,unittest.TestCase):
    SITE='ministryofjustice-gr';CLASS='MinistryOfJusticeGrCrawler';LIST_MODULE=False
class TourismCounts(HtmlGreekPressContract,unittest.TestCase):
    SITE='mintour-gov-gr-deltia-typoy';CLASS='MintourGovGrDeltiaTypoyCrawler';LIST_MODULE=False
class EducationCounts(HtmlGreekPressContract,unittest.TestCase):
    SITE='minedu-gov-gr-grafeio-typoy-kai-di';CLASS='MineduGovGrGrafeioTypoyKaiDiCrawler';LIST_MODULE=True

for cls in (YpesCounts,MinscfaCounts,JusticeCounts,TourismCounts,EducationCounts):
    pg=type(cls.__name__+'Postgres',(cls,),{'BACKEND':'postgres'})
    globals()[pg.__name__]=unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')(pg)
del cls,pg

@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class GreekPressWorkerLifecycle(unittest.TestCase):
    def test_native_parsers_report_new_rows_failures_and_cancel_through_actual_worker(self):
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        for cls in (YpesCounts,MinscfaCounts,JusticeCounts,TourismCounts,EducationCounts):
            with self.subTest(site=cls.SITE):
                case=cls(methodName='test_repeat_three_reaches_remainder_and_then_zero');case.BACKEND='postgres'
                try:
                    case.setUp();init_delivery_schema(case.conn)
                    for limit,cancel,expected in [(3,None,3),(3,None,3),(None,2,2),(None,None,0)]:
                        c=case.crawler();before=case.count()
                        jid=jobs.enqueue_job(case.conn,case.SITE,mode='full',limit_n=limit)
                        job=jobs.claim_next_job(case.conn)
                        def should_cancel():
                            if cancel and case.count()>=before+cancel:jobs.cancel_job(case.conn,jid);return True
                            return False
                        returned=worker.run_job(case.conn,job,{case.SITE:lambda **kw:c},delay=0,should_cancel=should_cancel)
                        detail=jobs.job_detail(case.conn,jid)['job']
                        self.assertEqual((returned,detail['saved_count'],detail['status']),(expected,expected,'cancelled' if cancel else 'done'))
                        self.assertEqual(case.count()-before,expected)
                    c=case.crawler();case.fetch.side_effect=lambda *a,**k:None
                    jid=jobs.enqueue_job(case.conn,case.SITE,mode='full')
                    self.assertEqual(worker.run_job(case.conn,jobs.claim_next_job(case.conn),{case.SITE:lambda **kw:c},delay=0),0)
                    detail=jobs.job_detail(case.conn,jid)['job']
                    self.assertEqual(detail['status'],'failed');self.assertIn('fetch_failed',detail['error']);self.assertEqual(case.count(),8)
                finally:case.doCleanups()
