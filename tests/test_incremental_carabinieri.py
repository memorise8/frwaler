import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from urllib.parse import parse_qs,urlsplit

from crawler import db_libertree
from crawler.incremental import Collector,Policy,Store,UnsupportedSource,init_schema
from crawler.incremental_carabinieri import CarabinieriAdapter
from crawler.sites import CRAWLERS
from delivery.incremental_registry import adapter_for


def listing(ids,page,last,d='all'):
    base=CarabinieriAdapter.endpoint
    def url(n):return base+(f'/!page/page/{n}' if n>1 else '')+'?d='+d
    body=''.join(f'<tr><td> {i:02}/10/2026 12:00 </td><td><a href="{base}/item-{i}">Title {i}</a></td><td>Original publisher</td><td>Actual excerpt {i}.</td></tr>' for i in ids)
    if not ids:body='<tr><td colspan="6">Nessun elemento trovato con i parametri indicati.</td></tr>'
    links=''.join(f'<a href="{url(i)}"'+(' class="sf_PagerCurrent"' if ids and i==page else '')+f'>{i}</a>' for i in range(1,last+1))
    return '<table class="tabella footable"><caption>Comunicati Stampa</caption><thead><tr>'+''.join(f'<th>{x}</th>' for x in ['Data','Titolo','Fonte','Descrizione'])+'</tr></thead><tbody>'+body+'</tbody></table><div class="PagerLista"><div class="sf_pagerNumeric">'+links+'</div></div>'


def detail(n):
    return f'<div class="docTitolo">Title {n}</div><div class="docData">Comando Provinciale di Roma, {n:02}/10/2026 12:00</div><div class="docTesto">Original body {n}.</div>'


class CarabinieriIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.conn=db_libertree.open_db(Path(self.tmp.name)/'owned.sqlite');self.addCleanup(self.conn.close)
        db_libertree.init_db(self.conn);init_schema(self.conn)
        self.cls=CRAWLERS['carabinieri-it-in-vostro-aiuto'];db_libertree.upsert_site(self.conn,self.cls.site_id,'Carabinieri',self.cls.base_url)
        self.enterContext(patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'sqlite'}))
        self.docs=list(range(12,0,-1));self.calls=[];self.policy=Policy(full_check_days=0)

    def request(self,url,**kw):
        self.calls.append(url);r=Mock();p=urlsplit(url)
        if '/item-' in p.path:r.text=detail(int(p.path.rsplit('-',1)[-1]));return r
        self.assertEqual(parse_qs(p.query),{'d':['all']})
        page=int(p.path.rsplit('/',1)[-1]) if '/!page/' in p.path else 1
        r.text=listing(self.docs[(page-1)*3:page*3],page,(len(self.docs)+2)//3);return r

    def instance(self):
        c=self.cls(self.conn,delay=0);c._request=self.request
        class Small(CarabinieriAdapter):rows=3
        return c,Small(c)

    def test_default_factory_preserves_tutti(self):
        a=adapter_for(self.cls(self.conn,delay=0));self.assertIsInstance(a,CarabinieriAdapter)
        self.assertEqual(a.scope['d'],'all')

    def test_full_oldest_first_delivery_and_repeat_without_details(self):
        c,a=self.instance();self.assertEqual(Collector(c,a,self.policy).run(),12)
        rows=list(self.conn.execute('SELECT post_number,published_date,abstract FROM documents ORDER BY seq_id'))
        self.assertEqual([r[0] for r in rows],[f'item-{n}' for n in range(1,13)])
        self.assertEqual(rows[0][1:3],('2026-10-01','Original body 1.'))
        self.calls=[];c,a=self.instance();collector=Collector(c,a,self.policy);self.assertEqual(collector.run(),0)
        self.assertEqual(len(self.calls),3);self.assertTrue(all('/item-' not in u for u in self.calls))
        self.assertEqual(collector.stats['stop_reason'],'known_boundary_plus_overlap')

    def test_partial_save_resumes_only_remaining_details(self):
        c,a=self.instance();self.assertEqual(Collector(c,a,self.policy).run(limit=3),3)
        self.assertFalse(Store(self.conn,c.site_id).state().get('baseline_complete',False))
        self.calls=[];c,a=self.instance();self.assertEqual(Collector(c,a,self.policy).run(),9)
        self.assertEqual(len(self.calls),9);self.assertTrue(all('/item-' in u for u in self.calls))

    def test_discovery_budget_resumes_after_anchor_validation(self):
        c,a=self.instance();Collector(c,a,Policy(max_pages=1,full_check_days=0)).run()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0],0)
        self.calls=[];c,a=self.instance();self.assertEqual(Collector(c,a,self.policy).run(),12)
        self.assertEqual(self.calls[:2],[a.url(1),a.url(2)])

    def test_new_record_after_baseline_requires_one_detail(self):
        c,a=self.instance();Collector(c,a,self.policy).run();self.docs.insert(0,13)
        self.calls=[];c,a=self.instance();self.assertEqual(Collector(c,a,self.policy).run(),1)
        self.assertEqual(sum('/item-' in u for u in self.calls),1)

    def test_terminal_claim_requires_explicit_empty_adjacent_page(self):
        self.docs=[3,2,1];c,a=self.instance();native=self.request
        def nonempty(url,**kw):
            r=native(url,**kw)
            if '/!page/page/2' in url:r.text=listing([4],2,2)
            return r
        c._request=nonempty
        with self.assertRaisesRegex(ValueError,'terminal pager contradicted'):Collector(c,a,self.policy).run()
        self.assertFalse(Store(self.conn,c.site_id).state().get('baseline_complete',False))

    def test_empty_without_source_message_is_not_completion(self):
        self.docs=[];c,a=self.instance();native=self.request
        c._request=lambda url,**kw:Mock(text=native(url,**kw).text.replace('Nessun elemento trovato con i parametri indicati.','Network error'))
        with self.assertRaisesRegex(ValueError,'not verified exhaustion'):Collector(c,a,self.policy).run()

    def test_explicit_initial_empty_query_is_observed_source_end(self):
        self.docs=[];c,a=self.instance();self.assertEqual(Collector(c,a,self.policy).run(),0)
        self.assertTrue(Store(self.conn,c.site_id).state()['baseline_complete'])

    def test_changed_anchor_prevents_advancing(self):
        c,a=self.instance();cursor=a.fetch(None,'native').next_cursor;self.docs[0]=13
        with self.assertRaisesRegex(ValueError,'previous page changed'):a.fetch(cursor,'native')

    def test_scope_leak_in_pager_is_rejected(self):
        c,a=self.instance();native=self.request
        c._request=lambda url,**kw:Mock(text=native(url,**kw).text.replace('?d=all','?d=202610'))
        with self.assertRaisesRegex(ValueError,'scope mismatch'):Collector(c,a,self.policy).run()

    def test_wrong_detail_date_is_retained_as_pending(self):
        c,a=self.instance();native=self.request
        c._request=lambda url,**kw:Mock(text=detail(13) if '/item-' in url else native(url,**kw).text)
        with self.assertRaisesRegex(ValueError,'detail/list date'):Collector(c,a,self.policy).run()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM crawl_incremental_pending').fetchone()[0],12)

    def test_unverified_order_is_classified_without_assuming_latest(self):
        self.docs.reverse();c,a=self.instance()
        with self.assertRaises(UnsupportedSource):Collector(c,a,self.policy).run()
        self.assertEqual(Store(self.conn,c.site_id).state()['support'],'full_rescan_required')

if __name__=='__main__':unittest.main()
