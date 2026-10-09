"""Real persistence across all delivered scopes, overlap and late source pages."""
import csv,os,unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs,urlsplit
from tests.test_final_indirect_counts import NemertesCounts as _NemertesCounts
from crawler.base_crawler import CrawlCancelled


class NemertesDeliveryScopes(_NemertesCounts):
    def crawler(self):
        c=super().crawler();original=self.fetch.side_effect;scopes=self.module._SCOPES
        # Eight distinct documents; scope 2 begins with a page overlapping scope 1.
        memberships=[[1,2],[1,2,3,4],[5],[6,7],[8]]
        def fetch(url,params=None):
            if '/bundles' in url:return original(url,params)
            index=scopes.index(params['scope']);page=params['page'];ids=memberships[index]
            if getattr(self,'empty_first',False) and index==0:ids=[]
            if getattr(self,'failed_scope',None)==index:return None
            part=ids[page*2:(page+1)*2];items=[]
            for n in part:
                meta={'dc.title':[{'value':f'Research {n}'}],'dc.description.abstract':[{'value':self.body(n)}],'dc.date.issued':[{'value':self.date(n)}]}
                items.append({'uuid':f'{n:08d}-0000-0000-0000-000000000000','handle':f'123456789/{n}','metadata':meta})
            return {'_embedded':{'items':items},'page':{'number':page,'totalPages':(len(ids)+1)//2,'totalElements':len(ids)}}
        self.fetch.side_effect=fetch;return c

    def test_configured_scopes_match_every_delivery_browse_url(self):
        source=Path(__file__).resolve().parents[1]/'docs/deliverables/test_URL_수집대조_20260802.csv'
        with source.open(encoding='utf-8-sig') as f:
            expected={parse_qs(urlsplit(r['URL주소']).query)['scope'][0] for r in csv.DictReader(f) if r['매칭크롤러(site_id)']==self.SITE}
        self.assertEqual(len(expected),5);self.assertEqual(set(self.module._SCOPES),expected)

    def test_all_scopes_and_overlap_only_page_do_not_hide_later_documents(self):
        c=self.crawler();self.assertEqual(c.crawl(),8);self.assertFalse(c.delivery_source_failure)
        calls=[x.kwargs['params'] for x in self.fetch.call_args_list if '/bundles' not in x.args[0]]
        self.assertEqual({x['scope'] for x in calls},set(self.module._SCOPES))
        self.assertEqual(sum('/bundles' in x.args[0] for x in self.fetch.call_args_list),8)
        before=self.identities();self.assertEqual(self.crawler().crawl(),0);self.assertEqual(self.identities(),before)

    def test_empty_scope_does_not_end_remaining_scopes(self):
        self.empty_first=True;c=self.crawler();self.assertEqual(c.crawl(),8);self.assertFalse(c.delivery_source_failure)

    def test_later_scope_fetch_failure_preserves_rows_and_reports_incomplete(self):
        self.failed_scope=2;c=self.crawler();self.assertEqual(c.crawl(),4);self.assertEqual(c.delivery_source_failure,'fetch_failed')
        self.failed_scope=None;self.assertEqual(self.crawler().crawl(),4);self.assertEqual(self.count(),8)

    def test_source_reported_pages_beyond200_are_traversed(self):
        c=self.crawler();original=self.fetch.side_effect;scope=self.module._SCOPES[0]
        def fetch(url,params=None):
            if '/bundles' in url:return original(url,params)
            if params['scope']!=scope:return {'page':{'number':0,'totalPages':0,'totalElements':0}}
            n=params['page']+1
            return {'_embedded':{'items':[{'uuid':f'{n:08d}-0000-0000-0000-000000000000','handle':f'123456789/{n}','metadata':{'dc.title':[{'value':f'Research {n}'}],'dc.description.abstract':[{'value':self.body(n)}]}}]},'page':{'number':params['page'],'totalPages':201,'totalElements':201}}
        self.fetch.side_effect=fetch
        self.assertEqual(c.crawl(),201);self.assertEqual(self.count(),201);self.assertFalse(c.delivery_source_failure)

    def test_premature_empty_page_is_incomplete(self):
        c=self.crawler();original=self.fetch.side_effect
        def fetch(url,params=None):
            r=original(url,params)
            if '/bundles' not in url and params['scope']==self.module._SCOPES[1] and params['page']==1:r['_embedded']['items']=[]
            return r
        self.fetch.side_effect=fetch;self.assertEqual(c.crawl(),2);self.assertEqual(c.delivery_source_failure,'invalid_response')

    def test_changed_scope_total_is_incomplete(self):
        c=self.crawler();original=self.fetch.side_effect
        def fetch(url,params=None):
            r=original(url,params)
            if '/bundles' not in url and params['page']==1:r['page']['totalElements']+=1
            return r
        self.fetch.side_effect=fetch;self.assertEqual(c.crawl(),2);self.assertEqual(c.delivery_source_failure,'invalid_response')

    # These inherited one-scope fixture assertions depend on its 5+3 page layout.
    def test_repeated_source_page_reports_incomplete(self):
        c=self.crawler();original=self.fetch.side_effect
        def fetch(url,params=None):
            if '/bundles' not in url and params['page']>0:return original(url,dict(params,page=0))
            return original(url,params)
        self.fetch.side_effect=fetch;self.assertEqual(c.crawl(),2);self.assertEqual(c.delivery_source_failure,'invalid_response')


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class NemertesDeliveryScopesPostgres(NemertesDeliveryScopes):
    BACKEND='postgres'


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class NemertesDeliveryScopeWorker(unittest.TestCase):
    def test_actual_worker_limit_cancel_and_resume_across_scopes(self):
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        case=NemertesDeliveryScopesPostgres(methodName='test_repeat_three_reaches_remainder_and_then_zero')
        try:
            case.setUp();init_delivery_schema(case.conn)
            for limit,cancel,expected in [(3,None,3),(3,None,3),(None,1,1),(None,None,1),(None,None,0)]:
                c=case.crawler();before=case.count();jid=jobs.enqueue_job(case.conn,case.SITE,mode='full',limit_n=limit)
                def should_cancel():
                    if cancel and case.count()>=before+cancel:jobs.cancel_job(case.conn,jid);return True
                    return False
                returned=worker.run_job(case.conn,jobs.claim_next_job(case.conn),{case.SITE:lambda **kw:c},delay=0,should_cancel=should_cancel)
                detail=jobs.job_detail(case.conn,jid)['job']
                self.assertEqual((returned,detail['saved_count'],detail['status']),(expected,expected,'cancelled' if cancel else 'done'))
                self.assertEqual(case.count()-before,expected)
            self.assertEqual(case.count(),8)
        finally:case.doCleanups()
