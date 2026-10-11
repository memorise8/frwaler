import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from urllib.parse import parse_qs,urlsplit

from crawler import db_libertree
from crawler.incremental import Collector,Policy,Store,UnsupportedSource,init_schema
from crawler.incremental_ens_lyon import EnsLyonAdapter
from crawler.sites import CRAWLERS
from delivery.incremental_registry import adapter_for

SITE='ens-lyon-fr-lecole';ROOT='https://www.ens-lyon.fr'
BODY='Les chercheurs étudient les données et publient les résultats de leur recherche. '*3


def fixtures():return list(range(13,0,-1))


def listing(docs,page):
    last=(len(docs)-1)//3;items=docs[page*3:page*3+3]
    cards=''.join(f'<article data-history-node-id="{2000-i}"><a href="/actualite/report-{i}?ctx=contexte"><h3><span class="field--name-title">Research {i}</span></h3><div class="field--name-field-article-date">Date de publication {i:02}/09/2026</div></a></article>' for i in items)
    pager=''.join(f'<a href="?page={n}"'+(' aria-current="true"' if n==min(page,last) else '')+f'>{n+1}</a>' for n in range(last+1))
    if page<last:pager+=f'<li class="pager__item--next"><a href="?page={page+1}" rel="next">Next</a></li>'
    return f'<h1>Communiqués de presse</h1><div class="view-communiques-de-presse">{cards}<nav class="pager">{pager}</nav></div>'


def detail(i,excluded=(1,)):
    body='Video' if i in excluded else BODY
    return f'<h1>Research {i}</h1><main><article data-history-node-id="{2000-i}"><div class="field--name-field-article-date">Date de publication {i:02}/09/2026</div><div class="field--name-field-article-resume">{body}</div></article></main>'


def response_for(url,docs,excluded=(1,),**kwargs):
    assert kwargs.get('headers',{}).get('Accept-Encoding')=='gzip, deflate'
    if '/actualite/report-' in url:return Mock(text=detail(int(url.rsplit('-',1)[-1]),excluded))
    return Mock(text=listing(docs,int(parse_qs(urlsplit(url).query)['page'][0])))


class EnsIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.conn=db_libertree.open_db(Path(self.tmp.name)/'owned.sqlite');self.addCleanup(self.conn.close)
        db_libertree.init_db(self.conn);init_schema(self.conn);self.cls=CRAWLERS[SITE]
        db_libertree.upsert_site(self.conn,SITE,'ENS',ROOT)
        self.enterContext(patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'sqlite'}));self.enterContext(patch.object(EnsLyonAdapter,'rows',3))
        self.docs=fixtures();self.excluded={1};self.calls=[];self.policy=Policy(full_check_days=0)

    def instance(self):
        c=self.cls(self.conn,delay=0);self.addCleanup(c._session.close)
        def request(url,**kw):self.calls.append(url);return response_for(url,self.docs,self.excluded,**kw)
        c._request=request;return c,adapter_for(c)

    def collect(self,limit=None):
        c,a=self.instance();col=Collector(c,a,self.policy);return col.run(limit),col

    def test_full_preserves_explicit_exclusion_and_uses_dates_not_numeric_ID_order(self):
        self.assertEqual(self.collect()[0],12);state=Store(self.conn,SITE).state()
        self.assertTrue(state['baseline_complete']);self.assertEqual(state['order'],'newest_first');self.assertEqual(state['order_evidence'],'date')
        self.assertEqual(state['cursor']['memberships'],13);self.assertEqual(state['cursor']['duplicates'],0)
        self.assertEqual(set(state['cursor']['exclusions']),{'1999'})
        rows=list(self.conn.execute('SELECT post_number,published_date_raw FROM documents ORDER BY seq_id'))
        self.assertEqual([r[0] for r in rows],[str(2000-i) for i in range(2,14)])
        self.assertEqual(rows[0][1],'Date de publication 02/09/2026')
        self.calls=[];saved,col=self.collect();self.assertEqual(saved,0);self.assertEqual(len(self.calls),3)
        self.assertEqual(col.stats['detail_requests'],0);self.assertEqual(col.stats['stop_reason'],'known_boundary_plus_overlap')
        self.assertEqual(set(Store(self.conn,SITE).state()['cursor']['exclusions']),{'1999'})

    def test_resume_revalidates_exclusion_without_refetching_eligible_details(self):
        self.assertEqual(self.collect(3)[0],3);state=Store(self.conn,SITE).state()
        self.assertFalse(state.get('baseline_complete',False));self.assertTrue(state['discovery_exhausted'])
        self.calls=[];self.assertEqual(self.collect()[0],9);self.assertEqual(self.calls,[ROOT+'/actualite/report-1'])

    def test_changed_exclusion_blocks_resumed_completion(self):
        self.collect(3);self.excluded=set()
        with self.assertRaisesRegex(ValueError,'exclusion evidence changed'):self.collect()
        self.assertFalse(Store(self.conn,SITE).state().get('baseline_complete',False))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM crawl_incremental_pending').fetchone()[0],9)

    def test_full_check_recovers_newly_eligible_excluded_document(self):
        self.collect();self.excluded=set();c,a=self.instance();c.delivery_mode='full'
        self.assertEqual(Collector(c,a,self.policy).run(),1)
        self.assertEqual(Store(self.conn,SITE).state()['cursor']['exclusions'],{})

    def test_new_heads_get_only_new_details(self):
        self.collect();self.docs=list(range(15,0,-1));self.calls=[];saved,col=self.collect()
        self.assertEqual(saved,2);self.assertEqual(col.stats['detail_requests'],2)
        self.assertEqual([r[0] for r in self.conn.execute('SELECT post_number FROM documents ORDER BY seq_id DESC LIMIT 2')],['1985','1986'])

    def test_exclusion_only_page_can_checkpoint_without_fake_document(self):
        self.excluded={13,12,11,1};c,a=self.instance();self.assertEqual(Collector(c,a,Policy(max_pages=1,full_check_days=0)).run(),0)
        state=Store(self.conn,SITE).state();self.assertEqual(state['phase'],'discover');self.assertEqual(state['cursor']['page'],1)
        self.assertEqual(self.collect()[0],9)

    def test_discovery_checkpoint_keeps_prepared_pages(self):
        c,a=self.instance();Collector(c,a,Policy(max_pages=1,full_check_days=0)).run();self.calls=[]
        self.assertEqual(self.collect()[0],12)
        self.assertFalse(any(u.endswith(('/report-13','/report-12','/report-11')) for u in self.calls))

    def test_failed_detail_does_not_become_short_body_exclusion(self):
        c,a=self.instance();native=c._request
        c._request=lambda url,**kw:Mock(text='<h1>Unavailable</h1>') if url.endswith('/report-13') else native(url,**kw)
        with self.assertRaisesRegex(ValueError,'detail failed'):Collector(c,a,self.policy).run()
        self.assertFalse(Store(self.conn,SITE).state().get('baseline_complete',False))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0],0)

    def test_partial_date_is_unsupported(self):
        c,a=self.instance();native=c._request
        c._request=lambda url,**kw:Mock(text=native(url,**kw).text.replace('13/09/2026','09/2026'))
        with self.assertRaises(UnsupportedSource):Collector(c,a,self.policy).run()

    def test_cross_page_inversion_is_unsupported(self):
        self.docs[3]=14
        with self.assertRaisesRegex(UnsupportedSource,'cross_page'):self.collect()

    def test_anchor_change_rejects_resume(self):
        c,a=self.instance();Collector(c,a,Policy(max_pages=1,full_check_days=0)).run();self.docs[0]=14
        with self.assertRaisesRegex(ValueError,'previous page changed'):self.collect()

    def test_adjacent_nonempty_page_is_not_exhaustion(self):
        c,a=self.instance();native=c._request
        c._request=lambda url,**kw:Mock(text=listing(self.docs,4)) if url.endswith('page=5') else native(url,**kw)
        with self.assertRaises(ValueError):Collector(c,a,self.policy).run()
        self.assertFalse(Store(self.conn,SITE).state().get('baseline_complete',False))

    def test_existing_document_cannot_silently_become_excluded(self):
        self.collect();self.excluded.add(2);c,a=self.instance();c.delivery_mode='full'
        with self.assertRaisesRegex(ValueError,'explicitly excluded'):Collector(c,a,self.policy).run()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0],12)

    def test_repeated_cross_page_membership_cannot_certify_incomplete_snapshot(self):
        self.docs.insert(3,11)
        with self.assertRaisesRegex(ValueError,'snapshot reconciliation required'):self.collect()
        self.assertFalse(Store(self.conn,SITE).state().get('baseline_complete',False))

    def test_conflicting_duplicate_membership_is_not_silently_merged(self):
        self.docs.insert(3,11)
        c,a=self.instance();native=c._request
        c._request=lambda url,**kw:Mock(text=native(url,**kw).text.replace('Research 11','Renamed 11')) if url.endswith('page=1') else native(url,**kw)
        with self.assertRaisesRegex(UnsupportedSource,'conflicting_duplicate'):Collector(c,a,self.policy).run()
        self.assertFalse(Store(self.conn,SITE).state().get('baseline_complete',False))

    def test_native_transport_is_restored_after_list_failure(self):
        c,a=self.instance();before=c._curl_get;c._request=Mock(return_value=Mock(text='<h1>Unavailable</h1>'))
        with self.assertRaises(ValueError):a.read(0)
        self.assertEqual(c._curl_get,before)

if __name__=='__main__':unittest.main()
