import copy
from datetime import date,timedelta
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from crawler import db_libertree
from crawler.incremental import Collector,Policy,Store,UnsupportedSource
from crawler.incremental_dspace import NemertesAdapter,NEMERTES_SCOPES
from crawler.sites import CRAWLERS
from delivery.incremental_registry import adapter_for


def item(n):
    return {'uuid':f'00000000-0000-0000-0000-{n:012d}','name':f'Title {n}', 'handle':f'10889/{n}',
            'type':'item','withdrawn':False,'inArchive':True,'discoverable':True,
            'metadata':{'dc.title':[{'value':f'Title {n}'}],
                        'dc.date.accessioned':[{'value':(date(2020,1,1)+timedelta(days=n)).isoformat()+'T12:00:00Z'}],
                        'dc.contributor.author':[{'value':'Last, First'}]}}


class DSpaceIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.conn=db_libertree.open_db(Path(self.tmp.name)/'owned.sqlite');self.addCleanup(self.conn.close)
        from crawler.incremental import init_schema
        db_libertree.init_db(self.conn);init_schema(self.conn)
        self.cls=CRAWLERS['nemertes-library-upatras-gr-browse'];db_libertree.upsert_site(self.conn,self.cls.site_id,'test',self.cls.base_url)
        self.enterContext(patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'sqlite'}))
        self.scopes=NEMERTES_SCOPES[:2];self.data={self.scopes[0]:[item(i) for i in [1,3,5,7,9,11]],self.scopes[1]:[item(i) for i in [2,4,6,8,10,12]]}
        self.calls=[];self.policy=Policy(full_check_days=0)

    def request(self,url,params=None,**kw):
        self.calls.append((url,params));response=Mock()
        if url.endswith('/bundles'):
            response.json.return_value={'page':{'totalElements':0,'totalPages':0}};return response
        self.assertTrue(url.endswith('/discover/browses/title/items'))
        self.assertEqual(params['sort'],'dateaccessioned,desc')
        vals=sorted(self.data[params['scope']],key=lambda d:d['metadata']['dc.date.accessioned'][0]['value'],reverse=True)
        n=len(vals);size=params['size'];page=params['page']
        response.json.return_value={'page':{'number':page,'size':size,'totalElements':n,'totalPages':(n+size-1)//size},
                                    '_embedded':{'items':copy.deepcopy(vals[page*size:(page+1)*size])}}
        return response

    def instance(self):
        c=self.cls(self.conn,delay=0);c._request=self.request
        class Small(NemertesAdapter):rows=3
        return c,Small(c,scopes=self.scopes)

    def test_default_registry_keeps_all_five_original_scopes(self):
        c=self.cls(self.conn,delay=0);a=adapter_for(c)
        self.assertIsInstance(a,NemertesAdapter)
        self.assertEqual(tuple(a.scope['scopes']),c.crawl.__func__.__globals__['_SCOPES'])

    def test_full_merge_saves_globally_oldest_first_and_repeats_without_details(self):
        c,a=self.instance();self.assertEqual(Collector(c,a,self.policy).run(),12)
        self.assertEqual([r[0] for r in self.conn.execute('SELECT post_number FROM documents ORDER BY seq_id')],[item(i)['uuid'] for i in range(1,13)])
        self.assertEqual(Store(self.conn,c.site_id).state()['order'],'newest_first')
        self.calls=[];c,a=self.instance();collector=Collector(c,a,self.policy)
        self.assertEqual(collector.run(),0)
        self.assertEqual(collector.stats['detail_requests'],0)
        self.assertTrue(all(not u.endswith('/bundles') for u,p in self.calls))
        self.assertEqual({p['scope'] for u,p in self.calls},set(self.scopes))
        self.assertEqual(collector.stats['stop_reason'],'known_boundary_plus_overlap')

    def test_new_item_in_second_scope_cannot_be_hidden_by_known_first_scope(self):
        c,a=self.instance();Collector(c,a,self.policy).run()
        self.data[self.scopes[1]].append(item(20));self.calls=[];c,a=self.instance()
        self.assertEqual(Collector(c,a,self.policy).run(),1)
        self.assertIsNotNone(self.conn.execute('SELECT 1 FROM documents WHERE post_number=?',(item(20)['uuid'],)).fetchone())
        self.assertEqual(sum(u.endswith('/bundles') for u,p in self.calls),1)

    def test_new_older_scope_head_survives_newer_known_scope_boundary(self):
        self.data={self.scopes[0]:[item(i) for i in range(20,32)],self.scopes[1]:[item(i) for i in range(1,7)]}
        c,a=self.instance();Collector(c,a,self.policy).run()
        self.data[self.scopes[1]].append(item(7));self.calls=[];c,a=self.instance()
        collector=Collector(c,a,self.policy)
        self.assertEqual(collector.run(),1)
        self.assertIsNotNone(self.conn.execute('SELECT 1 FROM documents WHERE post_number=?',(item(7)['uuid'],)).fetchone())
        self.assertEqual(sum(u.endswith('/bundles') for u,p in self.calls),1)
        self.assertGreater(collector.stats['listed'],6)

    def test_scope_overlap_is_deduplicated(self):
        self.data[self.scopes[1]].append(item(11));c,a=self.instance()
        self.assertEqual(Collector(c,a,self.policy).run(),12)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0],12)

    def test_incremental_checkpoint_preserves_unemitted_new_scope_head(self):
        self.data={self.scopes[0]:[item(i) for i in range(20,32)],self.scopes[1]:[item(i) for i in range(1,7)]}
        c,a=self.instance();Collector(c,a,self.policy).run()
        self.data[self.scopes[1]].append(item(7));self.calls=[];c,a=self.instance()
        self.assertEqual(Collector(c,a,Policy(max_pages=1,full_check_days=0)).run(),0)
        self.assertEqual(Store(self.conn,c.site_id).state()['phase'],'discover')
        c,a=self.instance();self.assertEqual(Collector(c,a,self.policy).run(),1)
        self.assertEqual(sum(u.endswith('/bundles') for u,p in self.calls),1)

    def test_discovery_budget_resumes_persisted_merge_heads(self):
        c,a=self.instance();Collector(c,a,Policy(max_pages=1,full_check_days=0)).run()
        self.assertFalse(Store(self.conn,c.site_id).state().get('baseline_complete',False))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0],0)
        c,a=self.instance();self.assertEqual(Collector(c,a,self.policy).run(),12)
        self.assertTrue(Store(self.conn,c.site_id).state()['baseline_complete'])

    def test_delivery_pause_retains_metadata_and_only_fetches_remaining_details(self):
        c,a=self.instance();self.assertEqual(Collector(c,a,self.policy).run(limit=3),3)
        self.calls=[];c,a=self.instance();self.assertEqual(Collector(c,a,self.policy).run(),9)
        self.assertTrue(all(u.endswith('/bundles') for u,p in self.calls));self.assertEqual(len(self.calls),9)
        self.assertEqual(self.conn.execute('SELECT authors FROM documents LIMIT 1').fetchone()[0],'Last, First')

    def test_bad_page_anchor_fails_without_advancing(self):
        c,a=self.instance();cursor=None
        cursor=a.fetch(None,'native').next_cursor
        self.data[self.scopes[0]][-1]['name']='edited'
        with self.assertRaisesRegex(ValueError,'previous page changed'):
            for _ in range(10):cursor=a.fetch(cursor,'native').next_cursor

    def test_missing_accession_date_is_explicitly_unsupported(self):
        c,a=self.instance();native=self.request
        def bad(url,params=None,**kw):
            r=native(url,params,**kw);d=r.json();d['_embedded']['items'][0]['metadata'].pop('dc.date.accessioned');r.json.return_value=d;return r
        c._request=bad
        with self.assertRaises(UnsupportedSource):Collector(c,a,self.policy).run()
        self.assertEqual(Store(self.conn,c.site_id).state()['support'],'full_rescan_required')

    def test_foreign_scope_is_rejected_before_requests(self):
        c,a=self.instance()
        with self.assertRaises(ValueError):NemertesAdapter(c,scopes=['unrelated'])
        self.assertEqual(self.calls,[])

if __name__=='__main__':unittest.main()
