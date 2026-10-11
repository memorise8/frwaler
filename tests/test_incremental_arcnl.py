import contextlib
from datetime import date,timedelta
import html
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests
from crawler import db_libertree
from crawler.incremental_arcnl import SCOPES, LABELS, list_url
from crawler.incremental import Collector,Policy,Store,init_schema
from crawler.incremental_arcnl import ARCNLAdapter,BASE
from crawler.sites import CRAWLERS
from delivery.incremental_registry import adapter_for

SITE='arcnl-nl-publications'
BODY='A controlled substantive law reform report body for storage and continuation testing.'


def response(url,raw,status=200):
    r=requests.Response();r.url=url;r.status_code=status;r._content=raw.encode();r.encoding='utf-8';return r


def stamp(i):return (date(2026,1,1)+timedelta(days=i)).isoformat()


def listing(ids,scope,page):
    last=(len(ids)+9)//10
    head=f'<html><head><link rel="canonical" href="{list_url(scope,page)}"></head><body class="term-{scope} paged-{page}"><h1>Publications - {LABELS[scope]}</h1>'
    if page>last:
        return head+'<div class="block__content"><p>No items found.</p></div></body></html>',200
    cards=''.join(f'<div class="item--publication"><meta itemprop="url" content="{BASE}/publications/{i}"><h3 class="item__title"><a href="{BASE}/publications/{i}">A. Author, <em>Report {i}</em>, Institute, {stamp(i)}</a></h3></div>' for i in ids[(page-1)*10:page*10])
    pager=''.join(f'<span class="page-numbers current">{n}</span>' if n==page else f'<a class="page-numbers" href="{list_url(scope,n)}">{n}</a>' for n in range(1,last+1))
    if page<last:pager+=f'<a class="next page-numbers" href="{list_url(scope,page+1)}">Next</a>'
    return head+cards+pager+'</body></html>',200


def detail(url,title,*,identity=None,body=BODY,stamp_value=None):
    if stamp_value is None:stamp_value=stamp(int(url.rsplit('/',1)[-1]))
    return f'<html><head><link rel="canonical" href="{html.escape(url)}"></head><body class="postid-{identity}"><div class="article__main"><h1>{html.escape(title)}</h1><div class="meta--publication"><table><tr><td>Publication date</td><td>{stamp_value}</td></tr></table></div><section class="excerpt">{body}</section></div></body></html>'


class Transport:
    def __init__(self):
        self.ids={SCOPES[0]:list(range(30,0,-1)),SCOPES[1]:list(range(60,29,-1)),SCOPES[2]:list(range(90,59,-1))}
        self.calls=[];self.bad_boundary=False;self.fail_detail=False;self.bad_identity=False
    def __call__(self,method,url,**kwargs):
        assert method.upper()=='GET';assert kwargs['allow_redirects'] is False;self.calls.append(url)
        for scope in SCOPES:
            for page in range(1,20):
                if url==list_url(scope,page):
                    raw,status=listing(self.ids[scope],scope,page)
                    if self.bad_boundary and page>(len(self.ids[scope])+9)//10:raw='<html><p>Unavailable</p></html>';status=200
                    return response(url,raw,status)
        i=int(url.rstrip('/').rsplit('/',1)[-1])
        if self.fail_detail:return response(url,'failure',503)
        return response(url,detail(BASE+'/wrong/' if self.bad_identity else url,f'Report {i}',identity=1000+i,stamp_value=stamp(i)))


class ARCNLIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.conn=db_libertree.open_db(Path(self.tmp.name)/'owned.sqlite');self.addCleanup(self.conn.close)
        db_libertree.init_db(self.conn);init_schema(self.conn);db_libertree.upsert_site(self.conn,SITE,'ARCNL',BASE)
        self.fixture=Transport();self.enterContext(patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'sqlite'}))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def instance(self):
        c=CRAWLERS[SITE](self.conn,delay=0);self.addCleanup(c._session.close);c._session.request=self.fixture
        return c,adapter_for(c)

    def collect(self,limit=None):
        c,a=self.instance();collector=Collector(c,a,Policy(full_check_days=0));return collector.run(limit),collector

    def test_full_discovery_oldest_save_resume_repeat(self):
        self.assertEqual(self.collect(3)[0],3);state=Store(self.conn,SITE).state()
        self.assertTrue(state['discovery_exhausted']);self.assertFalse(state.get('baseline_complete',False))
        pending=self.conn.execute('SELECT payload FROM crawl_incremental_pending WHERE item_key=?',(BASE+'/publications/30',)).fetchone()
        self.assertEqual(json.loads(pending[0])['_scope'],'bachelorthesis')
        self.fixture.calls.clear();self.assertEqual(self.collect()[0],87)
        self.assertTrue(all('/publications/' in u and '/types/' not in u for u in self.fixture.calls))
        self.assertEqual([r[0] for r in self.conn.execute('SELECT post_number FROM documents ORDER BY seq_id')],[str(1000+i) for i in range(1,91)])
        self.fixture.calls.clear();saved,col=self.collect();self.assertEqual(saved,0);self.assertEqual(col.stats['detail_requests'],0)
        self.assertEqual(col.stats['stop_reason'],'known_boundary_plus_overlap')
        self.assertFalse(any('/publications/' in u and '/types/' not in u for u in self.fixture.calls))
        self.assertEqual(Store(self.conn,SITE).state()['order_evidence'],'date')

    def test_new_item_in_other_scope_cannot_hide_behind_known_first_scope(self):
        self.collect();self.fixture.ids[SCOPES[2]].insert(0,91)
        self.assertEqual(self.collect()[0],1)
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM documents WHERE post_number='1091'").fetchone())

    def test_duplicate_in_overlap_is_rejected(self):
        self.collect();self.fixture.ids[SCOPES[0]].insert(11,19)
        # A duplicate cannot masquerade as a new stable item at an overlap.
        with self.assertRaises(ValueError):self.collect()

    def test_two_newly_visible_rows_in_overlap_are_saved_oldest_first(self):
        original=list(self.fixture.ids[SCOPES[0]])
        self.fixture.ids[SCOPES[0]]=[i for i in original if i not in (18,19)]
        self.collect();self.fixture.ids[SCOPES[0]]=original
        self.assertEqual(self.collect()[0],2)
        self.assertEqual([r[0] for r in self.conn.execute('SELECT post_number FROM documents ORDER BY seq_id DESC LIMIT 2')],['1019','1018'])

    def test_adjacent_wrong_shape_never_completes(self):
        self.fixture.bad_boundary=True
        with self.assertRaises(ValueError):self.collect()
        self.assertFalse(Store(self.conn,SITE).state().get('baseline_complete',False))

    def test_failed_detail_retains_pending_then_retries_oldest(self):
        self.fixture.fail_detail=True
        with self.assertRaises(requests.HTTPError):self.collect()
        self.assertFalse(Store(self.conn,SITE).state().get('baseline_complete',False))
        self.fixture.fail_detail=False;self.fixture.calls.clear();self.assertEqual(self.collect()[0],90)
        self.assertTrue(all('/publications/' in u and '/types/' not in u for u in self.fixture.calls))

    def test_canonical_identity_failure_never_saves(self):
        self.fixture.bad_identity=True
        with self.assertRaisesRegex(ValueError,'identity'):self.collect()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0],0)

    def test_oldest_source_never_claims_newest_access(self):
        self.fixture.ids={s:list(reversed(v)) for s,v in self.fixture.ids.items()}
        self.collect();self.assertEqual(Store(self.conn,SITE).state()['support'],'full_rescan_required')

    def test_short_abstract_is_not_silently_marked_complete(self):
        original=self.fixture.__call__
        def transport(method,url,**kwargs):
            r=original(method,url,**kwargs)
            if '/types/' not in url:r._content=r.content.replace(BODY.encode(),b'short')
            return r
        c,a=self.instance();c._session.request=transport
        with self.assertRaisesRegex(ValueError,'short_abstract'):Collector(c,a,Policy(full_check_days=0)).run()
        state=Store(self.conn,SITE).state();self.assertFalse(state.get('baseline_complete',False))
        self.assertEqual(state['support'],'full_rescan_required')

    def test_wrong_page_or_missing_date_cannot_complete_baseline(self):
        c,a=self.instance();raw,_=listing(list(range(30,0,-1)),SCOPES[0],1)
        with self.assertRaises(ValueError):a.inspect(raw,SCOPES[0],2)
        with self.assertRaisesRegex(ValueError,'date'):a.inspect(raw.replace(stamp(30),'2026'),SCOPES[0],1)

    def test_periodic_full_reports_changes_without_deleting_or_overwriting(self):
        self.collect();before=[tuple(r) for r in self.conn.execute('SELECT * FROM documents ORDER BY seq_id')]
        self.fixture.ids[SCOPES[0]].remove(1)
        c,a=self.instance();native=self.fixture.__call__
        def transport(method,url,**kwargs):
            r=native(method,url,**kwargs)
            if url==BASE+'/publications/2':r._content=r.content.replace(BODY.encode(),(BODY+' Updated.').encode())
            return r
        c._session.request=transport
        state=Store(self.conn,SITE).state();state['last_full_at']=1;Store(self.conn,SITE).checkpoint(state)
        self.assertEqual(Collector(c,a,Policy(full_check_days=1)).run(),0)
        self.assertEqual(before,[tuple(r) for r in self.conn.execute('SELECT * FROM documents ORDER BY seq_id')])
        changes={(r['item_key'],r['kind']) for r in self.conn.execute('SELECT item_key,kind FROM crawl_incremental_changes')}
        self.assertIn((BASE+'/publications/1','missing'),changes)
        self.assertIn((BASE+'/publications/2','modified'),changes)

    def test_equal_date_internal_reordering_keeps_page_anchor(self):
        c,a=self.instance();native=self.fixture.__call__
        def transport(method,url,**kwargs):
            r=native(method,url,**kwargs)
            r._content=r.content.replace(stamp(30).encode(),stamp(29).encode())
            return r
        c._session.request=transport;items,last,anchor=a.read(SCOPES[0],1)
        self.fixture.ids[SCOPES[0]][:2]=[29,30]
        reordered,last2,anchor2=a.read(SCOPES[0],1)
        self.assertNotEqual(items,reordered);self.assertEqual(anchor,anchor2)

    def test_native_fetch_error_is_reported_and_not_source_exhaustion(self):
        from types import SimpleNamespace
        c,a=self.instance()
        with patch.object(c.crawl.__globals__['subprocess'],'run',return_value=SimpleNamespace(returncode=22,stdout='upstream failure')) as run,patch('time.sleep'):
            self.assertEqual(c.crawl(),0);self.assertEqual(run.call_count,3)
        self.assertEqual(c.delivery_source_failure,'fetch_failed');self.assertFalse(c.delivery_exhausted)

    def test_native_challenge_is_reported(self):
        from tests.test_incremental_accc import DENIAL
        c,a=self.instance()
        with patch.dict(c.crawl.__globals__,{'_curl_get':lambda url:DENIAL}):self.assertEqual(c.crawl(),0)
        self.assertEqual(c.delivery_source_failure,'authentication_failed')


if __name__=='__main__':unittest.main()
