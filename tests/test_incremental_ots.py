import copy
from datetime import datetime,timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from crawler import db_libertree
from crawler.incremental import Collector,Policy,Store,UnsupportedSource,init_schema
from crawler.incremental_ots import OTSAdapter
from crawler.sites import CRAWLERS
from delivery.incremental_registry import adapter_for


def item(n):
    day=f'2026-01-{n:02}'
    return {'key':f'OTS_{day.replace("-","")}_OTS0001','date':day,'time':'12:00:00',
            'timestamp':str(int(datetime.fromisoformat(day+'T12:00:00+00:00').timestamp())),
            'emittentId':'54','emittentName':'Original publisher','titleSlug':f'title-{n}',
            'title':{'content':f'Title {n}'},'lead':{'content':'Short'},
            'article':[{'type':'text','content':f'Body {n}'}],'keywords':['Public policy']}


class OTSIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.conn=db_libertree.open_db(Path(self.tmp.name)/'owned.sqlite');self.addCleanup(self.conn.close)
        db_libertree.init_db(self.conn);init_schema(self.conn)
        self.cls=CRAWLERS['ots-at-pressemappe'];db_libertree.upsert_site(self.conn,self.cls.site_id,'OTS',self.cls.base_url)
        self.enterContext(patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'sqlite'}))
        self.docs=[item(i) for i in range(12,0,-1)];self.calls=[];self.policy=Policy(full_check_days=0)

    def request(self,url,params=None,**kw):
        self.calls.append((url,params));r=Mock()
        if '/pressrelease/' in url:
            doc=next(d for d in self.docs if d['key']==url.rsplit('/',1)[-1])
            r.json.return_value={'error':[],'result':copy.deepcopy(doc)};return r
        self.assertEqual(params['emittentId'],'54');self.assertEqual(params['query'],'')
        n=len(self.docs);page=params['page'];size=params['maxCountPerPage'];docs=self.docs[(page-1)*size:page*size]
        r.json.return_value={'error':[],'meta':{'endpoint':'search','count':len(docs),'totalCount':str(n),'page':str(page),
                            'totalPages':(n+size-1)//size,'maxCountPerPage':str(size) if n else 0,'emittentId':'54'},'result':copy.deepcopy(docs)}
        return r

    def instance(self):
        c=self.cls(self.conn,delay=0);c._request=self.request
        class Small(OTSAdapter):rows=3
        return c,Small(c)

    def test_default_registry_keeps_whole_original_emitter(self):
        a=adapter_for(self.cls(self.conn,delay=0))
        self.assertIsInstance(a,OTSAdapter);self.assertEqual(a.scope['query'],'');self.assertEqual(a.scope['emittentId'],'54')

    def test_latest_full_pass_then_repeat_stops_without_detail_calls(self):
        c,a=self.instance();self.assertEqual(Collector(c,a,self.policy).run(),12)
        keys=[r[0] for r in self.conn.execute('SELECT post_number FROM documents ORDER BY seq_id')]
        self.assertEqual(keys,[item(i)['key'] for i in range(1,13)])
        self.assertEqual(Store(self.conn,c.site_id).state()['order'],'newest_first')
        self.calls=[];c,a=self.instance();collector=Collector(c,a,self.policy);self.assertEqual(collector.run(),0)
        self.assertEqual(len(self.calls),3);self.assertEqual(collector.stats['detail_requests'],0)
        self.assertEqual(collector.stats['stop_reason'],'known_boundary_plus_overlap')

    def test_oldest_source_is_inferred_and_repeat_reads_from_last_page(self):
        self.docs.reverse();c,a=self.instance();Collector(c,a,self.policy).run()
        self.assertEqual(Store(self.conn,c.site_id).state()['order'],'oldest_first')
        self.docs.append(item(13));self.calls=[];c,a=self.instance();collector=Collector(c,a,self.policy)
        self.assertEqual(collector.run(),1)
        self.assertEqual([p['page'] for u,p in self.calls if p],[1,5,5,4,4,3])
        self.assertEqual(collector.stats['detail_requests'],1)

    def test_delivery_resume_only_fetches_pending_details(self):
        c,a=self.instance();self.assertEqual(Collector(c,a,self.policy).run(limit=3),3)
        self.assertFalse(Store(self.conn,c.site_id).state().get('baseline_complete',False))
        self.calls=[];c,a=self.instance();self.assertEqual(Collector(c,a,self.policy).run(),9)
        self.assertEqual(len(self.calls),9);self.assertTrue(all('/pressrelease/' in u for u,p in self.calls))
        self.assertTrue(Store(self.conn,c.site_id).state()['baseline_complete'])

    def test_discovery_budget_does_not_complete_and_resumes_next_page(self):
        c,a=self.instance();Collector(c,a,Policy(max_pages=1,full_check_days=0)).run()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0],0)
        self.calls=[];c,a=self.instance();self.assertEqual(Collector(c,a,self.policy).run(),12)
        self.assertEqual([p['page'] for u,p in self.calls if p][:2],[1,2])

    def test_anchor_mutation_stops_before_advancing(self):
        c,a=self.instance();cursor=a.fetch(None,'native').next_cursor
        self.docs[0]['title']['content']='changed'
        with self.assertRaisesRegex(ValueError,'previous page changed'):a.fetch(cursor,'native')

    def test_scope_leak_is_rejected(self):
        self.docs[0]['emittentId']='99';c,a=self.instance()
        with self.assertRaisesRegex(ValueError,'emitter scope'):Collector(c,a,self.policy).run()
        self.assertFalse(Store(self.conn,c.site_id).state().get('baseline_complete',False))

    def test_missing_timestamp_is_explicit_full_rescan_hold(self):
        self.docs[0].pop('timestamp');c,a=self.instance()
        with self.assertRaises(UnsupportedSource):Collector(c,a,self.policy).run()
        self.assertEqual(Store(self.conn,c.site_id).state()['support'],'full_rescan_required')

    def test_duplicate_identity_is_not_a_source_end(self):
        self.docs[1]=copy.deepcopy(self.docs[0]);c,a=self.instance()
        with self.assertRaisesRegex(ValueError,'duplicate'):Collector(c,a,self.policy).run()
        self.assertFalse(Store(self.conn,c.site_id).state().get('baseline_complete',False))

    def test_wrong_detail_stays_pending(self):
        c,a=self.instance();native=self.request
        def wrong(url,params=None,**kw):
            r=native(url,params,**kw)
            if '/pressrelease/' in url:r.json.return_value={'error':[],'result':item(20)}
            return r
        c._request=wrong
        with self.assertRaisesRegex(ValueError,'identity mismatch'):Collector(c,a,self.policy).run()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM crawl_incremental_pending').fetchone()[0],12)
        c,a=self.instance();self.assertEqual(Collector(c,a,self.policy).run(),12)

    def test_query_change_cannot_reuse_unfiltered_baseline(self):
        c,a=self.instance();Collector(c,a,self.policy).run();c,a=self.instance()
        with self.assertRaisesRegex(ValueError,'contract changed'):Collector(c,OTSAdapter(c,query='Phishing'),self.policy).run()

    def test_sparse_numeric_article_object_preserves_paragraph_order(self):
        self.docs[-1]['article']={'10':{'type':'text','content':'Tenth.'},'2':{'type':'text','content':'Second.'},'0':{'type':'text','content':'First.'}}
        c,a=self.instance();Collector(c,a,self.policy).run()
        self.assertEqual(self.conn.execute('SELECT abstract FROM documents ORDER BY seq_id LIMIT 1').fetchone()[0],
                         'Short First. Second. Tenth.')

    def test_empty_original_body_is_saved_without_padding(self):
        self.docs[-1]['article']=[];self.docs[-1]['lead']={'content':''}
        c,a=self.instance();Collector(c,a,self.policy).run()
        self.assertEqual(self.conn.execute('SELECT abstract FROM documents ORDER BY seq_id LIMIT 1').fetchone()[0],'')

    def test_nonnumeric_article_object_does_not_silently_drop_body(self):
        self.docs[-1]['article']={'unknown':{'type':'text','content':'Must not disappear'}}
        c,a=self.instance()
        with self.assertRaisesRegex(ValueError,'numeric metadata'):Collector(c,a,self.policy).run()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0],0)

    def test_bad_count_envelope_is_not_exhaustion(self):
        c,a=self.instance();native=self.request
        def bad(url,params=None,**kw):
            r=native(url,params,**kw);r.json.return_value['meta']['count']=999;return r
        c._request=bad
        with self.assertRaisesRegex(ValueError,'page/count'):Collector(c,a,self.policy).run()
        self.assertFalse(Store(self.conn,c.site_id).state().get('baseline_complete',False))

if __name__=='__main__':unittest.main()
