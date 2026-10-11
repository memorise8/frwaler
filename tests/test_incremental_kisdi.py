import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from urllib.parse import parse_qs,urlsplit

from crawler import db_libertree
from crawler.incremental import Collector,Policy,Store,UnsupportedSource,init_schema
from crawler.incremental_kisdi import KisdiAdapter
from crawler.sites import CRAWLERS
from delivery.incremental_registry import adapter_for
from tests.test_kisdi_delivery_scopes import listing,detail,SCOPES


def fixtures():
    # Journal dates deliberately precede all the other scopes.
    return {'basic':[str(n) for n in range(18,12,-1)],'premium':[str(n) for n in range(12,6,-1)],
            'journal':[str(n) for n in range(6,0,-1)]}


def response_for(url,docs,*,method='GET',data=None,**kwargs):
    q=parse_qs(urlsplit(url).query);scope=next(s for s,(_,m) in SCOPES.items() if m==q['arrMasterId'][0])
    if '/list.do' in url:
        raw=listing(scope,int(q['pageIndex'][0]),ids=docs[scope],size=3)
        # Per-item dates from stable fixture identities, never from production IDs.
        for n in docs[scope]:
            old=f'<strong>연구 {n}</strong><ul><li><strong>발행일</strong>2026-08-31'
            raw=raw.replace(old,f'<strong>연구 {n}</strong><ul><li><strong>발행일</strong>2026-08-{int(n):02}')
    else:
        assert method=='POST' and data=={'pageIndex':'1'}
        n=q['artId'][0];raw=detail(n,date=f'2026-08-{int(n):02}')
    return Mock(text=raw)


class KisdiIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.conn=db_libertree.open_db(Path(self.tmp.name)/'owned.sqlite');self.addCleanup(self.conn.close)
        db_libertree.init_db(self.conn);init_schema(self.conn)
        self.cls=CRAWLERS['kisdi-re-kr-report'];db_libertree.upsert_site(self.conn,self.cls.site_id,'KISDI',self.cls.base_url)
        self.enterContext(patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'sqlite'}))
        self.enterContext(patch.object(KisdiAdapter,'rows',3))
        self.docs=fixtures();self.calls=[];self.policy=Policy(full_check_days=0)

    def instance(self):
        c=self.cls(self.conn,delay=0)
        def request(url,**kw):self.calls.append(url);return response_for(url,self.docs,**kw)
        c._request=request;return c,adapter_for(c)

    def run_collector(self,limit=None):
        c,a=self.instance();collector=Collector(c,a,self.policy);return collector.run(limit),collector

    def test_all_delivered_scopes_oldest_first_and_repeat_without_details(self):
        saved,_=self.run_collector();self.assertEqual(saved,18)
        rows=list(self.conn.execute('SELECT post_number FROM documents ORDER BY seq_id'))
        self.assertEqual([r[0] for r in rows],[str(i) for i in range(1,19)])
        self.calls=[];saved,col=self.run_collector();self.assertEqual(saved,0)
        self.assertEqual(len(self.calls),9);self.assertTrue(all('/list.do' in u for u in self.calls))
        self.assertEqual(col.stats['stop_reason'],'known_boundary_plus_overlap')

    def test_partial_save_resumes_only_remaining_details(self):
        self.assertEqual(self.run_collector(3)[0],3)
        self.assertFalse(Store(self.conn,self.cls.site_id).state().get('baseline_complete',False))
        self.calls=[];self.assertEqual(self.run_collector()[0],15)
        self.assertEqual(len(self.calls),15);self.assertTrue(all('/view.do' in u for u in self.calls))

    def test_new_item_in_older_scope_not_hidden_by_newer_known_scopes(self):
        self.run_collector();self.docs['journal'].insert(0,'19')
        # A newly assigned ID can have a date older than other masters' known heads.
        c,a=self.instance();native=c._request
        def request(url,**kw):
            r=native(url,**kw)
            if 'arrMasterId=5850223' in url:r.text=r.text.replace('2026-08-19','2026-08-07')
            return r
        c._request=request;self.calls=[];collector=Collector(c,a,self.policy)
        self.assertEqual(collector.run(),1)
        self.assertEqual(sum('/view.do' in u for u in self.calls),1)
        self.assertEqual(self.conn.execute("SELECT published_date FROM documents WHERE post_number='19'").fetchone()[0],'2026-08-07')
        self.assertGreater(collector.stats['listed'],6) # default shared boundary would have stopped at 6

    def test_cross_master_duplicate_does_not_silently_merge(self):
        self.docs['journal'][0]='12'
        with self.assertRaisesRegex(UnsupportedSource,'cross_master_identity'):self.run_collector()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0],0)

    def test_missing_body_keeps_pending_and_prevents_baseline(self):
        c,a=self.instance();native=c._request;c._abstract_from_pdf=Mock(side_effect=ValueError('source PDF text unavailable'))
        def request(url,**kw):
            if '/view.do' in url:self.calls.append(url);return Mock(text=detail('1',body=''))
            return native(url,**kw)
        c._request=request
        with self.assertRaisesRegex(ValueError,'PDF text unavailable'):Collector(c,a,self.policy).run()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM crawl_incremental_pending').fetchone()[0],18)
        self.assertFalse(Store(self.conn,c.site_id).state().get('baseline_complete',False))

    def test_unverified_date_order_classified(self):
        self.docs['basic'].reverse()
        with self.assertRaises(UnsupportedSource):self.run_collector()
        self.assertEqual(Store(self.conn,self.cls.site_id).state()['support'],'full_rescan_required')

    def test_changed_anchor_rejects_resume(self):
        c,a=self.instance();Collector(c,a,Policy(max_pages=1,full_check_days=0)).run()
        self.docs['basic'][0]='19'
        with self.assertRaisesRegex(ValueError,'previous page changed'):self.run_collector()

    def test_periodic_full_inspects_details_without_deleting_documents(self):
        self.run_collector();before=list(self.conn.execute('SELECT * FROM documents ORDER BY seq_id'));self.calls=[]
        c,a=self.instance();c.delivery_mode='full';self.assertEqual(Collector(c,a,self.policy).run(),0)
        self.assertEqual(sum('/view.do' in u for u in self.calls),18)
        self.assertEqual(list(self.conn.execute('SELECT * FROM documents ORDER BY seq_id')),before)

if __name__=='__main__':unittest.main()
