import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from urllib.parse import parse_qs,urlsplit

from crawler import db_libertree
from crawler.incremental import Collector,Policy,Store,UnsupportedSource,init_schema
from crawler.incremental_bankofcanada import BankOfCanadaAdapter,SCOPES
from crawler.sites import CRAWLERS
from delivery.incremental_registry import adapter_for


def listing(scope,ids,page,last,*,dates=True,total=None):
    rows=[]
    for n in ids:
        date=f'<span class="media-date">January {n}, 2026</span>' if dates else ''
        rows.append(f'<article class="media" id="post-{n}">{date}<h3 class="media-heading"><a href="https://www.bankofcanada.ca/2026/01/item-{n}/" data-content-type="Research">Title {n}</a></h3><div class="media-authors"><a>Author {n}</a></div><div class="media-excerpt">Original excerpt {n}.</div><div class="media-tags"><a>Source tag</a></div></article>')
    return f'<html><body class="postid-{scope["source_page_id"]}"><div id="{scope["widget_id"]}" class="mtw archive">'+''.join(rows)+f'<span class="num-results">{total}</span><div class="pagination"><span class="current">{page}</span></div><input type="number" min="1" max="{last}" value="{page}"></div></body></html>'


def detail(n):
    return f'<html><head><link rel="canonical" href="https://www.bankofcanada.ca/2026/01/item-{n}/"><meta name="publication_date" content="2026-01-{n:02}"><meta property="og:title" content="Title {n}"><meta name="description" content="Original description {n}."></head><body class="postid-{n}"><main><h1>Title {n}</h1><p>Actual original body {n}.</p></main></body></html>'


class BankIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.conn=db_libertree.open_db(Path(self.tmp.name)/'owned.sqlite');self.addCleanup(self.conn.close)
        db_libertree.init_db(self.conn);init_schema(self.conn)
        self.cls=CRAWLERS['bankofcanada-ca-publications'];db_libertree.upsert_site(self.conn,self.cls.site_id,'Bank',self.cls.base_url)
        self.enterContext(patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'sqlite'}))
        self.scopes=copy.deepcopy(SCOPES[:2])
        for s in self.scopes:s['rows_per_page']=3
        self.enterContext(patch('crawler.incremental_bankofcanada.SCOPES',self.scopes))
        self.docs={'scope-0':[11,9,7,5,3,1],'scope-1':[12,10,8,6,4,2]};self.calls=[]
        self.policy=Policy(full_check_days=0)

    def request(self,url,**kw):
        self.calls.append(url);r=Mock();p=urlsplit(url);q=parse_qs(p.query)
        if '/item-' in p.path:r.text=detail(int(p.path.rstrip('/').rsplit('-',1)[-1]));return r
        scope=next(s for s in self.scopes if urlsplit(s['archive_url']).path==p.path)
        page=int(q.get('mt_page',['1'])[0]);ids=self.docs[scope['scope']];size=scope['rows_per_page']
        r.text=listing(scope,ids[(page-1)*size:page*size],page,(len(ids)+size-1)//size,dates=scope['scope']!='scope-1',total=len(ids));return r

    def instance(self):
        c=self.cls(self.conn,delay=0);c._request=self.request
        class Small(BankOfCanadaAdapter):rows=3
        return c,Small(c)

    def test_actual_default_registry_contains_all_nine_delivered_scopes(self):
        with patch('crawler.incremental_bankofcanada.SCOPES',SCOPES):
            a=adapter_for(self.cls(self.conn,delay=0))
        self.assertEqual(len(a.scope['scopes']),9)
        self.assertEqual([s['delivered_url'] for s in a.scope['scopes']],[s['delivered_url'] for s in SCOPES])

    def test_full_merge_then_repeat_uses_known_research_dates_without_details(self):
        c,a=self.instance();self.assertEqual(Collector(c,a,self.policy).run(),12)
        rows=list(self.conn.execute('SELECT post_number,keywords FROM documents ORDER BY seq_id'))
        self.assertEqual([r[0] for r in rows],[str(i) for i in range(1,13)])
        self.assertTrue(all('consumer expectations' not in r[1] for r in rows))
        self.calls=[];c,a=self.instance();collector=Collector(c,a,self.policy);self.assertEqual(collector.run(),0)
        self.assertTrue(all('/item-' not in u for u in self.calls));self.assertEqual(collector.stats['detail_requests'],0)
        self.assertEqual(collector.stats['stop_reason'],'known_boundary_plus_overlap')

    def test_new_research_in_second_scope_is_discovered_and_fetched_once(self):
        c,a=self.instance();Collector(c,a,self.policy).run();self.docs['scope-1'].insert(0,13)
        self.calls=[];c,a=self.instance();self.assertEqual(Collector(c,a,self.policy).run(),1)
        self.assertEqual(sum('/item-' in u for u in self.calls),1)

    def test_partial_delivery_resumes_without_list_or_prefetched_detail_replay(self):
        c,a=self.instance();self.assertEqual(Collector(c,a,self.policy).run(limit=3),3)
        self.assertFalse(Store(self.conn,c.site_id).state().get('baseline_complete',False))
        self.calls=[];c,a=self.instance();self.assertEqual(Collector(c,a,self.policy).run(),9)
        self.assertTrue(all('/item-' in u for u in self.calls));self.assertEqual(len(self.calls),4)
        self.assertTrue(Store(self.conn,c.site_id).state()['baseline_complete'])

    def test_discovery_cursor_keeps_prefetched_papers(self):
        c,a=self.instance();Collector(c,a,Policy(max_pages=1,full_check_days=0)).run()
        self.assertFalse(Store(self.conn,c.site_id).state().get('baseline_complete',False))
        first=set(u for u in self.calls if '/item-' in u);self.calls=[]
        c,a=self.instance();self.assertEqual(Collector(c,a,self.policy).run(),12)
        self.assertFalse(first.intersection(u for u in self.calls if '/item-' in u))

    def test_periodic_full_rechecks_research_details(self):
        c,a=self.instance();Collector(c,a,self.policy).run();self.calls=[]
        c,a=self.instance();c.delivery_mode='full';self.assertEqual(Collector(c,a,self.policy).run(),0)
        self.assertEqual(sum('/item-' in u for u in self.calls),12)

    def test_missing_precise_detail_date_is_not_replaced_by_month_first(self):
        c,a=self.instance();native=self.request
        def missing(url,**kw):
            r=native(url,**kw)
            if '/item-' in url:r.text=r.text.replace('name="publication_date"','name="unavailable"')
            return r
        c._request=missing
        with self.assertRaises(UnsupportedSource):Collector(c,a,self.policy).run()
        self.assertEqual(Store(self.conn,c.site_id).state()['support'],'full_rescan_required')

    def test_changed_anchor_retains_checkpoint(self):
        c,a=self.instance();cursor=a.fetch(None,'native').next_cursor;self.docs['scope-0'][0]=13
        with self.assertRaisesRegex(ValueError,'previous page changed'):
            for _ in range(10):cursor=a.fetch(cursor,'native').next_cursor

    def test_cross_scope_duplicates_save_one_document(self):
        self.docs['scope-1'].insert(1,11);c,a=self.instance()
        self.assertEqual(Collector(c,a,self.policy).run(),12)

    def test_source_without_page_position_does_not_certify_completion(self):
        c,a=self.instance();native=self.request
        def missing(url,**kw):
            r=native(url,**kw);r.text=r.text.replace('class="current"','class="missing"');return r
        c._request=missing
        with self.assertRaisesRegex(ValueError,'page position'):Collector(c,a,self.policy).run()
        self.assertFalse(Store(self.conn,c.site_id).state().get('baseline_complete',False))

    def test_external_research_is_not_dropped_or_used_as_completion_evidence(self):
        c,a=self.instance();native=self.request
        def external(url,**kw):
            r=native(url,**kw)
            if '/research/' in url:r.text=r.text.replace('https://www.bankofcanada.ca/2026/01/item-12/','https://www.oar-rao.bank-banque-canada.ca/record/1516?ln=en&v=pdf')
            return r
        c._request=external
        with self.assertRaisesRegex(UnsupportedSource,'external_detail'):Collector(c,a,self.policy).run()
        state=Store(self.conn,c.site_id).state()
        self.assertEqual(state['support'],'full_rescan_required')
        self.assertFalse(state.get('baseline_complete',False))
        self.assertFalse(any('oar-rao' in u for u in self.calls))

if __name__=='__main__':unittest.main()
