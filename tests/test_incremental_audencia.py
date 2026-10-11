import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from urllib.parse import parse_qs,urlsplit

from crawler import db_libertree
from crawler.incremental import Collector, Policy, Store, UnsupportedSource, init_schema
from crawler.incremental_audencia import AudenciaAdapter
from crawler.sites import CRAWLERS
from delivery.incremental_registry import adapter_for

SITE='audencia-com-lecole'
ROOT='https://www.audencia.com'
BODY='La formation et la recherche soutiennent les étudiants et les entreprises. ' * 3


def listing(docs,page,category=None):
    size=3; total=len(docs);last=max(0,(total-1)//size);ids=docs[page*size:page*size+size]
    def link(n):return '?' + (f'categories={category}&' if category else '') + f'page={n}'
    cards=''.join(f'<div class="Card -news"><a class="Card-inner" href="/actualites/item-{i}"><h2 class="Card-title">Publication {i}</h2><time datetime="2026-09-{i:02}T12:00:00+02:00">{i:02}/09/2026</time><div class="TagWrapper"><span>Formations</span></div></a></div>' for i in ids)
    pager=''.join(f'<a href="{link(n)}"'+(' aria-current="page"' if n==min(page,last) else '')+f'>{n+1}</a>' for n in range(last+1))
    if page<last:pager+=f'<li class="pager__item--next"><a href="{link(page+1)}">Next</a></li>'
    empty='' if ids else '<div class="view-empty">Votre recherche ne retourne aucun résultat.</div>'
    return f'<div class="view-id-recherche_avancee view-display-id-press_releases"><span class="total-view-results">{total}</span><input name="categories" value="{category or "All"}" checked>{cards}{empty}<nav class="pager">{pager}</nav></div>'


def detail(i, *, body=BODY, node=None):
    settings=json.dumps({'path':{'currentPath':f'node/{node or 1000+i}'}})
    return f'<link rel="canonical" href="{ROOT}/actualites/item-{i}"><meta property="og:title" content="Publication {i}"><script data-drupal-selector="drupal-settings-json" type="application/json">{settings}</script><article class="node--type-actualite"><div class="Page-subtitle"><time>{i:02}/09/2026</time></div><div class="Page-content"><div class="field--name-field-text-content">{body}</div></div><a href="/files/paper-{i}.pdf">Télécharger</a></article>'


def response_for(url,docs,**kwargs):
    assert kwargs.get('headers',{}).get('Accept-Encoding')=='gzip, deflate'
    p=urlsplit(url)
    if '/actualites/item-' in p.path:return Mock(text=detail(int(p.path.rsplit('-',1)[-1])))
    q=parse_qs(p.query);return Mock(text=listing(docs,int(q['page'][0]),q.get('categories',[None])[0]))


class AudenciaIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.conn=db_libertree.open_db(Path(self.tmp.name)/'owned.sqlite');self.addCleanup(self.conn.close)
        db_libertree.init_db(self.conn);init_schema(self.conn)
        self.cls=CRAWLERS[SITE];db_libertree.upsert_site(self.conn,SITE,'Audencia',ROOT)
        self.enterContext(patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'sqlite'}))
        self.enterContext(patch.object(AudenciaAdapter,'rows',3))
        self.docs=list(range(12,0,-1));self.calls=[];self.policy=Policy(full_check_days=0)

    def instance(self,**kw):
        c=self.cls(self.conn,delay=0);self.addCleanup(c._session.close)
        def request(url,**kwargs):self.calls.append(url);return response_for(url,self.docs,**kwargs)
        c._request=request
        return c,AudenciaAdapter(c,**kw) if kw else adapter_for(c)

    def collect(self,limit=None):
        c,a=self.instance();col=Collector(c,a,self.policy);return col.run(limit),col

    def test_full_then_repeat_uses_URL_discovery_and_native_node_ids(self):
        self.assertEqual(self.collect()[0],12)
        rows=list(self.conn.execute('SELECT post_number,meta_url,published_date,pdf_url FROM documents ORDER BY seq_id'))
        self.assertEqual([r[0] for r in rows],[str(1000+i) for i in range(1,13)])
        self.assertEqual(rows[0][1:],(ROOT+'/actualites/item-1','2026-09-01',ROOT+'/files/paper-1.pdf'))
        state=Store(self.conn,SITE).state();self.assertTrue(state['baseline_complete']);self.assertEqual(state['order'],'newest_first')
        self.calls=[];saved,col=self.collect();self.assertEqual(saved,0);self.assertEqual(len(self.calls),3)
        self.assertEqual(col.stats['detail_requests'],0);self.assertEqual(col.stats['stop_reason'],'known_boundary_plus_overlap')

    def test_partial_then_resume_has_no_repeated_list_or_saved_detail(self):
        self.assertEqual(self.collect(3)[0],3);state=Store(self.conn,SITE).state()
        self.assertFalse(state.get('baseline_complete',False));self.assertTrue(state['discovery_exhausted'])
        self.calls=[];self.assertEqual(self.collect()[0],9);self.assertEqual(len(self.calls),9)
        self.assertTrue(all('/actualites/' in u for u in self.calls))

    def test_two_new_heads_have_only_two_detail_requests(self):
        self.collect();self.docs[:0]=[14,13];self.calls=[]
        saved,col=self.collect();self.assertEqual(saved,2);self.assertEqual(col.stats['detail_requests'],2)
        self.assertEqual([r[0] for r in self.conn.execute('SELECT post_number FROM documents ORDER BY seq_id DESC LIMIT 2')],['1014','1013'])

    def test_category_scope_is_echoed_and_kept_in_every_pager(self):
        c,a=self.instance(category='56');self.assertEqual(Collector(c,a,self.policy).run(),12)
        self.assertTrue(all('categories=56' in u for u in self.calls if '/actualites/' not in u))
        with self.assertRaisesRegex(ValueError,'filter changed'):a.pager_number('?page=1')

    def test_ignored_category_is_not_scoped_completion(self):
        c,a=self.instance(category='56');native=c._request
        c._request=lambda url,**kw:Mock(text=native(url,**kw).text.replace('value="56"','value="All"'))
        with self.assertRaisesRegex(ValueError,'filter not confirmed'):Collector(c,a,self.policy).run()

    def test_end_requires_adjacent_empty_response(self):
        c,a=self.instance();native=c._request
        c._request=lambda url,**kw:Mock(text=listing(self.docs,3)) if url.endswith('page=4') else native(url,**kw)
        with self.assertRaisesRegex(ValueError,'boundary mismatch'):Collector(c,a,self.policy).run()
        self.assertFalse(Store(self.conn,SITE).state().get('baseline_complete',False))

    def test_missing_next_and_total_are_not_silent_exhaustion(self):
        c,a=self.instance();native=c._request
        c._request=lambda url,**kw:Mock(text=native(url,**kw).text.replace('pager__item--next','unrelated'))
        with self.assertRaises(ValueError):Collector(c,a,self.policy).run()

    def test_unknown_HTML_is_not_empty_source(self):
        c,a=self.instance();c._request=lambda *args,**kw:Mock(text='<h1>Unavailable</h1>')
        with self.assertRaisesRegex(ValueError,'press view missing'):Collector(c,a,self.policy).run()

    def test_incomplete_day_date_is_unsupported(self):
        c,a=self.instance();native=c._request
        c._request=lambda url,**kw:Mock(text=native(url,**kw).text.replace('2026-09-12T12:00:00+02:00','2026-09'))
        with self.assertRaises(UnsupportedSource):Collector(c,a,self.policy).run()

    def test_cross_page_date_inversion_is_unsupported(self):
        self.docs[3]=13
        with self.assertRaisesRegex(UnsupportedSource,'cross_page_order_changed'):self.collect()

    def test_anchor_change_retains_pending_discovery(self):
        c,a=self.instance();Collector(c,a,Policy(max_pages=1,full_check_days=0)).run();self.docs[0]=13
        with self.assertRaisesRegex(ValueError,'previous page changed'):self.collect()

    def test_duplicate_card_is_not_silently_skipped(self):
        self.docs[1]=12
        with self.assertRaisesRegex(UnsupportedSource,'duplicate_list_identities'):self.collect()

    def test_failed_oldest_detail_keeps_entire_pending_queue(self):
        c,a=self.instance();native=c._request
        c._request=lambda url,**kw:Mock(text=detail(1,body='')) if url.endswith('item-1') else native(url,**kw)
        with self.assertRaisesRegex(ValueError,'body gap'):Collector(c,a,self.policy).run()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM crawl_incremental_pending').fetchone()[0],12)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0],0)
        self.calls=[];self.assertEqual(self.collect()[0],12);self.assertEqual(len(self.calls),12)

    def test_canonical_mismatch_rejects_wrong_document(self):
        c,a=self.instance();native=c._request
        c._request=lambda url,**kw:Mock(text=native(url,**kw).text.replace('rel="canonical" href="'+ROOT,'rel="canonical" href="https://unrelated.example'))
        with self.assertRaisesRegex(ValueError,'identity mismatch'):Collector(c,a,self.policy).run()

    def test_changed_storage_id_is_not_inserted_as_duplicate(self):
        self.collect();c,a=self.instance();c.delivery_mode='full';native=c._request
        c._request=lambda url,**kw:Mock(text=detail(1,node=9001)) if url.endswith('item-1') else native(url,**kw)
        with self.assertRaisesRegex(UnsupportedSource,'changed_stored_identity'):Collector(c,a,self.policy).run()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0],12)

    def test_full_recheck_records_edits_and_preserves_existing_rows(self):
        self.collect();before=list(self.conn.execute('SELECT * FROM documents ORDER BY seq_id'));c,a=self.instance();c.delivery_mode='full';native=c._request
        c._request=lambda url,**kw:Mock(text=detail(1,body=BODY+' Revised.')) if url.endswith('item-1') else native(url,**kw)
        self.assertEqual(Collector(c,a,self.policy).run(),0);self.assertEqual(a.detail_requests,12)
        self.assertEqual(list(self.conn.execute('SELECT * FROM documents ORDER BY seq_id')),before)
        self.assertEqual(self.conn.execute("SELECT kind FROM crawl_incremental_changes WHERE item_key=?",(ROOT+'/actualites/item-1',)).fetchone()[0],'modified')

if __name__=='__main__':unittest.main()
