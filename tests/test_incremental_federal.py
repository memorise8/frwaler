import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from crawler import db_libertree
from crawler.incremental import Collector, Policy, Store, Page
from crawler.incremental_federal import FederalAdapter
from crawler.sites import CRAWLERS
from delivery.incremental_registry import adapter_for


class FederalIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.conn=db_libertree.open_db(Path(self.tmp.name)/'test.sqlite');self.addCleanup(self.conn.close)
        from crawler.incremental import init_schema
        db_libertree.init_db(self.conn);init_schema(self.conn)
        self.cls=CRAWLERS['federalregister-gov-documents']
        db_libertree.upsert_site(self.conn,self.cls.site_id,'test',self.cls.base_url)
        self.enterContext(patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'sqlite'}))
        self.docs=[{'document_number':str(i), 'html_url':f'https://www.federalregister.gov/documents/{i}',
                    'title':str(i),'publication_date':f'2026-01-{i:02}', 'abstract':''} for i in range(1,13)]
        self.calls=[];self.fail_scope=False;self.capped=False

    def request(self,url,params=None,**kwargs):
        if params:url+='?'+urlencode(params)
        p={k:v[0] for k,v in parse_qs(urlsplit(url).query).items()};self.calls.append(p)
        rows=int(p['per_page']);page=int(p['page']);reverse=p['order']=='newest'
        lower=p.get('conditions[publication_date][gte]');upper=p.get('conditions[publication_date][lte]')
        docs=[d for d in self.docs if (not lower or d['publication_date']>=lower) and (not upper or d['publication_date']<=upper)]
        docs.sort(key=lambda d:d['publication_date'],reverse=reverse)
        count=len(docs)
        values=docs[(page-1)*rows:page*rows]
        next_url=None
        if page*rows<count:
            nxt=dict(p,page=str(page+1),search_after_cursor='test-'+str(page+1),format='json')
            if self.fail_scope:nxt['conditions[agencies][]']='another-agency'
            next_url=self.cls._API_BASE+'?'+urlencode(nxt)
        response=Mock();response.json.return_value={'results':values,'count':count,'next_page_url':next_url}
        return response

    def instance(self,leaf=8,rows=3):
        c=self.cls(self.conn,delay=0);c._PAGE_SIZE=rows;c._request=self.request
        a=FederalAdapter(c);a.leaf_limit=leaf
        return c,a

    def test_native_registry_routes_new_adapter(self):
        c,a=self.instance()
        self.assertIsInstance(adapter_for(c),FederalAdapter)

    def test_partitioned_full_then_two_known_pages_with_no_details(self):
        c,a=self.instance();policy=Policy(full_check_days=0)
        self.assertEqual(Collector(c,a,policy).run(),12)
        state=Store(self.conn,c.site_id).state()
        self.assertEqual(state['order'],'newest_first');self.assertTrue(state['baseline_complete'])
        self.assertEqual([r[0] for r in self.conn.execute('SELECT post_number FROM documents ORDER BY seq_id')],list(map(str,range(1,13))))
        full_requests=len(self.calls);self.calls=[]
        c,a=self.instance();collector=Collector(c,a,policy)
        self.assertEqual(collector.run(),0)
        self.assertLess(len(self.calls),full_requests)
        self.assertEqual(collector.stats['save_attempts'],0)
        self.assertEqual(collector.stats['stop_reason'],'known_boundary_plus_overlap')
        self.assertFalse(c.delivery_exhausted)

    def test_save_limit_resumes_queue_without_network_or_deletion(self):
        c,a=self.instance();policy=Policy(full_check_days=0)
        self.assertEqual(Collector(c,a,policy).run(limit=2),2)
        self.assertFalse(Store(self.conn,c.site_id).state().get('baseline_complete'))
        self.calls=[];c,a=self.instance()
        self.assertEqual(Collector(c,a,policy).run(),10)
        self.assertEqual(self.calls,[])

    def test_same_publisher_number_different_urls_are_distinct(self):
        self.docs[1]['document_number']=self.docs[0]['document_number']
        c,a=self.instance()
        self.assertEqual(Collector(c,a,Policy(full_check_days=0)).run(),12)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM documents WHERE post_number='1'").fetchone()[0],2)
        c,a=self.instance()
        self.assertEqual(Collector(c,a,Policy(full_check_days=0)).run(),0)

    def test_changed_next_scope_rejected_before_following(self):
        self.fail_scope=True;c,a=self.instance()
        with self.assertRaisesRegex(ValueError,'scope|query'):Collector(c,a).run()
        self.assertFalse(Store(self.conn,c.site_id).state().get('baseline_complete',False))
        self.assertTrue(all(p['conditions[agencies][]']==c._AGENCY_SLUG for p in self.calls))

    def test_anchor_drift_rejects_resume(self):
        c,a=self.instance();policy=Policy(max_pages=3,full_check_days=0)
        Collector(c,a,policy).run()
        self.assertIsNotNone(Store(self.conn,c.site_id).state()['cursor']['leaf'])
        self.docs[-1]['title']='changed'
        c,a=self.instance()
        with self.assertRaisesRegex(ValueError,'anchor changed'):Collector(c,a,policy).run()
        self.assertFalse(Store(self.conn,c.site_id).state().get('baseline_complete',False))

    def test_structural_checkpoints_do_not_consume_overlap(self):
        from tests.test_shared_incremental import Source,Crawler
        self.conn.execute('CREATE TABLE docs(id TEXT PRIMARY KEY,payload TEXT)')
        c=Crawler(self.conn);c.saved_order=[]
        Collector(c,Source(c,list(range(1,16))),Policy(full_check_days=0)).run()
        c=Crawler(self.conn);c.saved_order=[];source=Source(c,list(range(1,16)));native=source.fetch
        def fetch(cursor,direction):
            if cursor and cursor.get('position')==3 and not cursor.get('control'):
                source.calls.append(('checkpoint',direction))
                return Page([],dict(cursor,control=True),evidence='validated split',checkpoint_only=True)
            return native(cursor,direction)
        source.fetch=fetch
        collector=Collector(c,source,Policy(full_check_days=0));collector.run()
        self.assertEqual(len(source.calls),3)
        self.assertEqual(collector.stats['listed'],6)

    def test_unadvertised_empty_structural_page_rejected(self):
        c,a=self.instance();a.fetch=lambda *args:Page([],{'page':2},checkpoint_only=True)
        with self.assertRaisesRegex(ValueError,'structural'):Collector(c,a).run()

    def test_source_total_cap_is_not_an_exact_completion_count(self):
        c,a=self.instance()
        # Directly exercise last leaf reconciliation with an independently
        # exhausted prefix beyond the API's 10,000 display/search window.
        a.read=lambda *args,**kw:{'count':0,'results':[],'next_page_url':None}
        cursor={'expected_total':10000,'count_is_lower_bound':True,'finished_count':109548,
                'ranges':[{'lower':'2026-01-01','upper':'2026-01-01'}],'leaf':None}
        self.assertTrue(a.fetch(cursor,'native').exhausted)
        cursor['count_is_lower_bound']=False
        with self.assertRaisesRegex(ValueError,'count evidence'):a.fetch(cursor,'native')

if __name__=='__main__':unittest.main()
