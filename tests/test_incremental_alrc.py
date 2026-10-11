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
from crawler.alrc_scope import SCOPES,list_url
from crawler.incremental import Collector,Policy,Store,init_schema
from crawler.incremental_alrc import ALRCAdapter,BASE
from crawler.sites import CRAWLERS
from delivery.incremental_registry import adapter_for

SITE='alrc-gov-au-publications'
BODY='A controlled substantive law reform report body for storage and continuation testing.'


def response(url,raw,status=200):
    r=requests.Response();r.url=url;r.status_code=status;r._content=raw.encode();r.encoding='utf-8';return r


def boundary(scope):
    if scope=='final-report':return '<html><div id="main-content"><main class="content"><section class="section-listing"><section class="post"><h1>Publications</h1><h2 class="post-subtitle">Final Report</h2></section><div class="section-listing-inner"></div></section></main></div></html>',200
    return '<html><div id="main-content"><h1>Page not found</h1><p>The requested page was not found.</p></div></html>',404


def stamp(i):return (date(2026,1,1)+timedelta(days=i)).isoformat()


def listing(ids,scope,page):
    last=(len(ids)+9)//10
    if page>last:return boundary(scope)
    cards=''.join(f'<div class="section-listing-item"><h2><a href="{BASE}/doc/{i}/">Report {i}</a></h2><span class="section-listing-item-date">{stamp(i)}</span><p>{BODY}</p></div>' for i in ids[(page-1)*10:page*10])
    pager=''.join(f'<span class="page current">{n}</span>' if n==page else f'<a class="page" href="{html.escape(list_url(scope,n))}">{n}</a>' for n in range(1,last+1))
    if page<last:pager+=f'<a class="next" href="{html.escape(list_url(scope,page+1))}">Next</a>'
    return '<html>'+cards+'<div class="tw-pagination">'+pager+'</div></html>',200


def detail(url,title,*,identity=None,body=BODY):
    identity_link=f'<link rel="shortlink" href="{BASE}/?p={identity}">' if identity else ''
    return f'<html><head><meta charset="utf-8"><link rel="canonical" href="{html.escape(url)}">{identity_link}</head><body><main><h1>{html.escape(title)}</h1><p>{body}</p></main></body></html>'


class Transport:
    def __init__(self):
        self.ids={SCOPES[0]:list(range(30,0,-1)),SCOPES[1]:list(range(60,29,-1))}
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
        return response(url,detail(BASE+'/wrong/' if self.bad_identity else url,f'Report {i}',identity=1000+i))


class ALRCIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.conn=db_libertree.open_db(Path(self.tmp.name)/'owned.sqlite');self.addCleanup(self.conn.close)
        db_libertree.init_db(self.conn);init_schema(self.conn);db_libertree.upsert_site(self.conn,SITE,'ALRC',BASE)
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
        pending=self.conn.execute('SELECT payload FROM crawl_incremental_pending WHERE item_key=?',(BASE+'/doc/30/',)).fetchone()
        self.assertEqual(json.loads(pending[0])['_scope'],'final-report')
        self.fixture.calls.clear();self.assertEqual(self.collect()[0],57)
        self.assertTrue(all('/doc/' in u for u in self.fixture.calls))
        self.assertEqual([r[0] for r in self.conn.execute('SELECT post_number FROM documents ORDER BY seq_id')],[str(1000+i) for i in range(1,61)])
        self.fixture.calls.clear();saved,col=self.collect();self.assertEqual(saved,0);self.assertEqual(col.stats['detail_requests'],0)
        self.assertEqual(col.stats['stop_reason'],'known_boundary_plus_overlap')
        self.assertFalse(any('/doc/' in u for u in self.fixture.calls))
        self.assertEqual(Store(self.conn,SITE).state()['order_evidence'],'date')

    def test_new_item_in_other_scope_cannot_hide_behind_known_first_scope(self):
        self.collect();self.fixture.ids[SCOPES[1]].insert(0,61)
        self.assertEqual(self.collect()[0],1)
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM documents WHERE post_number='1061'").fetchone())

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
        self.fixture.fail_detail=False;self.fixture.calls.clear();self.assertEqual(self.collect()[0],60)
        self.assertTrue(all('/doc/' in u for u in self.fixture.calls))

    def test_canonical_identity_failure_never_saves(self):
        self.fixture.bad_identity=True
        with self.assertRaisesRegex(ValueError,'identity'):self.collect()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0],0)

    def test_oldest_source_never_claims_newest_access(self):
        self.fixture.ids={s:list(reversed(v)) for s,v in self.fixture.ids.items()}
        self.collect();self.assertEqual(Store(self.conn,SITE).state()['support'],'full_rescan_required')


if __name__=='__main__':unittest.main()
