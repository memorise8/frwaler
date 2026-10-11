import copy
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from urllib.parse import parse_qs,urlsplit

from crawler import db_libertree
from crawler.incremental import Collector,Policy,Store,UnsupportedSource,init_schema
from crawler.incremental_open_science import OpenScienceAdapter
from crawler.sites import CRAWLERS
from delivery.incremental_registry import adapter_for

SCOPES=CRAWLERS['open-science-canada-ca-communities'].COMMUNITY_IDS


def item(n):
    from tests.test_open_science_delivery_scopes import OpenScienceDeliveryScopes
    x=OpenScienceDeliveryScopes.item(SimpleNamespace(dates=['2025']*50),n)
    x['metadata']['dc.date.accessioned'][0]['value']=f'2026-08-{n:02}T12:00:00Z'
    return x


def structural(n):
    return {'uuid':f'ffffffff-0000-0000-0000-{n:012d}','type':'collection','metadata':{},'name':'Structural collection'}


def fixtures():
    return {SCOPES[0]:[item(i) for i in range(18,12,-1)]+[structural(1)],
            SCOPES[1]:[item(i) for i in range(6,0,-1)]+[structural(2)]}


def response_for(url,docs,params=None,**kw):
    if params:
        q={k:str(v) for k,v in params.items()}
    else:q={k:v[0] for k,v in parse_qs(urlsplit(url).query).items()}
    r=Mock()
    if '/search/objects' in url:
        scope=q['scope'];values=docs[scope];page=int(q['page']);size=int(q['size']);assert q['sort']=='dc.date.accessioned,DESC'
        r.json.return_value={'scope':scope,'query':q['query'],'sort':{'by':'dc.date.accessioned','order':'DESC'},
            '_embedded':{'searchResult':{'_embedded':{'objects':[{'_embedded':{'indexableObject':copy.deepcopy(v)}} for v in values[page*size:(page+1)*size]]},
                'page':{'number':page,'size':size,'totalPages':(len(values)+size-1)//size,'totalElements':len(values)}}}}
    elif '/bundles?' in url:
        r.json.return_value={'_embedded':{'bundles':[]},'page':{'number':0,'size':20,'totalPages':0,'totalElements':0},'_links':{'self':{'href':url}}}
    elif '/core/items/' in url:
        r.json.return_value=item(int(url.rsplit('/',1)[-1][:8]))
    else:r.text='<main></main>'
    return r


class OpenScienceIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.conn=db_libertree.open_db(Path(self.tmp.name)/'owned.sqlite');self.addCleanup(self.conn.close)
        db_libertree.init_db(self.conn);init_schema(self.conn)
        self.cls=CRAWLERS['open-science-canada-ca-communities'];db_libertree.upsert_site(self.conn,self.cls.site_id,'Open Science',self.cls.base_url)
        self.enterContext(patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'sqlite'}));self.enterContext(patch.object(OpenScienceAdapter,'rows',3))
        self.docs=fixtures();self.calls=[];self.policy=Policy(full_check_days=0)

    def instance(self):
        c=self.cls(self.conn,delay=0)
        def request(url,**kw):self.calls.append(url);return response_for(url,self.docs,**kw)
        c._request=request;return c,adapter_for(c)

    def run_collector(self,limit=None):
        c,a=self.instance();collector=Collector(c,a,self.policy);return collector.run(limit),collector

    def test_two_original_scopes_full_order_dates_and_structures(self):
        self.assertEqual(self.run_collector()[0],12)
        rows=list(self.conn.execute('SELECT post_number,published_date_raw,listed_date_raw FROM documents ORDER BY seq_id'))
        self.assertEqual([r[0] for r in rows],[item(i)['uuid'] for i in list(range(1,7))+list(range(13,19))])
        self.assertTrue(all(r[1]=='2025' for r in rows));self.assertEqual(rows[0][2],'2026-08-01T12:00:00Z')
        state=Store(self.conn,self.cls.site_id).state();self.assertTrue(state['baseline_complete']);self.assertEqual(state['order_evidence'],'date')
        self.calls=[];saved,col=self.run_collector();self.assertEqual(saved,0)
        self.assertEqual(len(self.calls),6);self.assertTrue(all('/search/objects' in u for u in self.calls))
        self.assertEqual(col.stats['stop_reason'],'known_boundary_plus_overlap')

    def test_partial_resume_does_not_refetch_list_or_inline_item(self):
        self.assertEqual(self.run_collector(3)[0],3);self.assertFalse(Store(self.conn,self.cls.site_id).state().get('baseline_complete',False))
        self.calls=[];self.assertEqual(self.run_collector()[0],9)
        self.assertEqual(len(self.calls),9);self.assertTrue(all('/bundles?' in u for u in self.calls))

    def test_new_older_scope_head_not_hidden_by_newer_known_scope(self):
        self.run_collector();self.docs[SCOPES[1]].insert(0,item(7));self.calls=[]
        self.assertEqual(self.run_collector()[0],1)
        self.assertEqual(sum('/bundles?' in u for u in self.calls),1)

    def test_structural_only_page_is_checkpoint_not_exhaustion(self):
        self.docs[SCOPES[0]]=[structural(3),structural(4),structural(5)]+self.docs[SCOPES[0]]
        c,a=self.instance();self.assertEqual(Collector(c,a,Policy(max_pages=1,full_check_days=0)).run(),0)
        state=Store(self.conn,c.site_id).state();self.assertEqual(state['phase'],'discover');self.assertFalse(state['discovery_exhausted'])
        self.assertEqual(self.run_collector()[0],12)

    def test_page_of_structures_at_end_counts_toward_source_total(self):
        self.run_collector();state=Store(self.conn,self.cls.site_id).state()
        self.assertEqual([len(s['seen_ids']) for s in state['cursor']['streams']],[7,7])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0],12)

    def test_missing_accession_is_not_replaced_by_publication_year(self):
        self.docs[SCOPES[0]][0]['metadata'].pop('dc.date.accessioned')
        with self.assertRaises(UnsupportedSource):self.run_collector()
        self.assertEqual(Store(self.conn,self.cls.site_id).state()['support'],'full_rescan_required')

    def test_wrong_order_echo_cannot_be_used_as_latest(self):
        c,a=self.instance();native=c._request
        def request(url,**kw):
            r=native(url,**kw);d=r.json();d['sort']={'by':'score','order':'DESC'};r.json.return_value=d;return r
        c._request=request
        with self.assertRaisesRegex(ValueError,'sort echo'):Collector(c,a,self.policy).run()

    def test_nonmonotonic_dates_require_full_rescan(self):
        self.docs[SCOPES[0]][:2]=list(reversed(self.docs[SCOPES[0]][:2]))
        with self.assertRaises(UnsupportedSource):self.run_collector()

    def test_anchor_change_retains_queued_entries(self):
        c,a=self.instance();Collector(c,a,Policy(max_pages=1,full_check_days=0)).run()
        self.docs[SCOPES[0]][0]['name']='Changed source'
        with self.assertRaisesRegex(ValueError,'previous page changed'):self.run_collector()

    def test_body_failure_keeps_pending_and_restores_native_transports(self):
        c,a=self.instance();old_json=c._curl_json;old_get=c._curl_get
        for docs in self.docs.values():
            for d in docs:
                if d['type']=='item':d['metadata']['dc.description.abstract'][0]['value']=''
        with self.assertRaisesRegex(ValueError,'source body unavailable'):Collector(c,a,self.policy).run()
        self.assertEqual(c._curl_json,old_json);self.assertEqual(c._curl_get,old_get)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM crawl_incremental_pending').fetchone()[0],12)
        self.assertFalse(Store(self.conn,c.site_id).state().get('baseline_complete',False))

    def test_full_check_revisits_bundles_and_preserves_existing_rows(self):
        self.run_collector();before=list(self.conn.execute('SELECT * FROM documents ORDER BY seq_id'));self.calls=[]
        c,a=self.instance();c.delivery_mode='full';self.assertEqual(Collector(c,a,self.policy).run(),0)
        self.assertEqual(sum('/bundles?' in u for u in self.calls),12)
        self.assertEqual(list(self.conn.execute('SELECT * FROM documents ORDER BY seq_id')),before)

if __name__=='__main__':unittest.main()
