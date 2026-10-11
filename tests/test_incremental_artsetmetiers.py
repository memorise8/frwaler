import contextlib
from datetime import date, timedelta
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests
from crawler import db_libertree
from crawler.incremental import Collector, Policy, Store, init_schema
from crawler.sites import CRAWLERS
from delivery.incremental_registry import adapter_for
from tests.test_incremental_accc import DENIAL

SITE='artsetmetiers-fr-fr'
BASE=CRAWLERS[SITE]._LIST_URL
BODY='A source press summary with enough text to satisfy the existing native eligibility rule and exercise durable storage without downloading PDFs.'


def listing(ids, page, short=(), modified=False):
    final=(len(ids)-1)//5; current=min(page,final)
    fields=''.join(f'<input name="{k}" value="">' for k in ('combine','created','created_1','date_from','date_to'))
    cards=''
    for i in ids[page*5:page*5+5]:
        stamp=date(2026,1,1)+timedelta(days=i)
        month=['Jan','Fev','Mar','Avr','Mai','Jun','Jul','Aoû','Sep','Oct','Nov','Dec'][stamp.month-1]
        body='short' if i in short else BODY+(' Updated' if modified and i==1 else '')
        cards+=f'<div class="views-row"><a href="https://s3-site.artsetmetiers.fr/public/2026-01/item-{i}.pdf"><h2>Press {i}</h2><p>{body}</p><span class="day">{stamp.day}</span><span class="month">{month}</span><span class="year">{stamp.year}</span></a></div>'
    if cards:cards='<div class="view-content">'+cards+'</div>'
    pager=f'<nav class="pager"><li class="is-active"><a href="?page={current}">Current</a></li>'
    if current<final:pager+=f'<li class="pager__item--next"><a href="?page={current+1}">Next</a></li><li class="pager__item--last"><a href="?page={final}">Last</a></li>'
    return f'<html><link rel="canonical" href="{BASE}"><h1>Espace Presse</h1><div class="view-id-espace_presse view-display-id-page_1"><form id="views-exposed-form-espace-presse-page-1" action="/fr/communiques-de-presse">{fields}</form>{cards}{pager}</nav></div><div class="views-row"><a href="https://example.test/partner">Footer</a></div></html>'


class Transport:
    def __init__(self):
        self.ids=list(range(25,0,-1)); self.calls=[]; self.short=set(); self.modified=False; self.bad_empty=False
    def __call__(self,method,url,**kwargs):
        assert method.upper()=='GET' and kwargs['allow_redirects'] is False
        assert urlsplit(url).path=='/fr/communiques-de-presse'
        self.calls.append(url); n=int(parse_qs(urlsplit(url).query).get('page',['0'])[0])
        raw=listing(self.ids,n,self.short,self.modified)
        if self.bad_empty and n*5>=len(self.ids):raw=raw.replace('Espace Presse','Service unavailable')
        r=requests.Response();r.url=url;r.status_code=200;r._content=raw.encode();r.encoding='utf-8';return r


class ArtsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.conn=db_libertree.open_db(Path(self.tmp.name)/'owned.sqlite');self.addCleanup(self.conn.close)
        db_libertree.init_db(self.conn);init_schema(self.conn);db_libertree.upsert_site(self.conn,SITE,'Arts',BASE)
        self.fixture=Transport();self.enterContext(patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'sqlite'}))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
    def instance(self):
        c=CRAWLERS[SITE](self.conn,delay=0);self.addCleanup(c._session.close);c._session.request=self.fixture
        return c,adapter_for(c)
    def collect(self,limit=None):
        c,a=self.instance();col=Collector(c,a,Policy(full_check_days=0));return col.run(limit),col

    def test_full_discovery_partial_delivery_resume_and_repeat(self):
        self.assertEqual(self.collect(3)[0],3);s=Store(self.conn,SITE).state()
        self.assertTrue(s['discovery_exhausted']);self.assertFalse(s.get('baseline_complete',False))
        self.assertEqual(len(self.fixture.calls),10)
        self.fixture.calls.clear();self.assertEqual(self.collect()[0],22);self.assertEqual(self.fixture.calls,[])
        self.fixture.calls.clear();saved,col=self.collect();self.assertEqual(saved,0);self.assertEqual(len(self.fixture.calls),3)
        self.assertEqual(col.stats['stop_reason'],'known_boundary_plus_overlap');self.assertEqual(col.stats['detail_requests'],0)
        self.assertEqual([r[0] for r in self.conn.execute('SELECT post_number FROM documents ORDER BY seq_id')],['item-'+str(i)+'.pdf' for i in range(1,26)])

    def test_new_items_stored_oldest_first_without_detail_requests(self):
        self.collect();self.fixture.ids=[27,26]+self.fixture.ids
        self.assertEqual(self.collect()[0],2)
        self.assertEqual([r[0] for r in self.conn.execute('SELECT post_number FROM documents ORDER BY seq_id DESC LIMIT 2')],['item-27.pdf','item-26.pdf'])

    def test_oldest_order_detected_without_assuming_newest(self):
        self.fixture.ids.reverse();self.collect();s=Store(self.conn,SITE).state()
        self.assertEqual(s['order'],'oldest_first');self.assertEqual(s['support'],'full_rescan_required')

    def test_two_missing_items_in_overlap_are_recovered(self):
        ids=list(self.fixture.ids);self.fixture.ids=[i for i in ids if i not in (17,18)]
        self.collect();self.fixture.ids=ids;self.fixture.calls.clear()
        self.assertEqual(self.collect()[0],2);self.assertGreater(len(self.fixture.calls),3)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0],25)

    def test_entire_short_summary_page_advances_without_fake_documents(self):
        self.fixture.short=set(range(11,16));self.assertEqual(self.collect()[0],20)
        s=Store(self.conn,SITE).state();self.assertTrue(s['baseline_complete']);self.assertEqual(s['cursor']['excluded'],5)
        self.assertEqual(self.collect()[0],0)

    def test_unverified_end_never_completes(self):
        self.fixture.bad_empty=True
        with self.assertRaisesRegex(ValueError,'shape'):self.collect()
        self.assertFalse(Store(self.conn,SITE).state().get('baseline_complete',False))

    def test_prior_page_drift_rejected(self):
        c,a=self.instance();p=a.fetch(None,'source');self.fixture.ids=[26]+self.fixture.ids
        with self.assertRaisesRegex(ValueError,'drift'):a.fetch(p.next_cursor,'source')

    def test_scope_duplicate_and_invalid_date_rejected(self):
        c,a=self.instance()
        with self.assertRaisesRegex(ValueError,'scope'):a.page_number('?page=1&combine=limited')
        self.fixture.ids[7]=self.fixture.ids[0]
        with self.assertRaisesRegex(ValueError,'identity_repeat'):self.collect()
        raw=listing([1,2,3],0).replace('class="year">2026','class="year">unknown')
        with self.assertRaisesRegex(ValueError,'date_missing'):a.inspect(raw,0)

    def test_periodic_full_records_changes_without_mutating_existing_documents(self):
        self.collect();self.fixture.modified=True;self.fixture.ids.remove(2)
        st=Store(self.conn,SITE);s=st.state();s['last_full_at']=0;st.checkpoint(s)
        c,a=self.instance();self.assertEqual(Collector(c,a,Policy(full_check_days=1)).run(),0)
        kinds={r[0] for r in self.conn.execute('SELECT kind FROM crawl_incremental_changes')}
        self.assertTrue({'missing','modified'}<=kinds)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0],25)
        self.assertEqual(self.conn.execute("SELECT abstract FROM documents WHERE post_number='item-1.pdf'").fetchone()[0],BODY)

    def test_save_interruption_keeps_pending_for_next_run(self):
        c,a=self.instance()
        with patch.object(c,'_save_paper',side_effect=RuntimeError('controlled failure')):
            with self.assertRaisesRegex(RuntimeError,'controlled'):Collector(c,a,Policy(full_check_days=0)).run()
        self.fixture.calls.clear();self.assertEqual(self.collect()[0],25);self.assertEqual(self.fixture.calls,[])

    def test_native_transport_and_access_failures_are_reported(self):
        c,a=self.instance()
        with patch.object(c.crawl.__globals__['subprocess'],'run',return_value=SimpleNamespace(returncode=22,stdout=b'error')) as run,patch('time.sleep'):
            self.assertIsNone(c._fetch_page_html(0));self.assertEqual(run.call_count,3)
        self.assertEqual(c.delivery_source_failure,'fetch_failed')
        c,a=self.instance()
        with patch.object(c.crawl.__globals__['subprocess'],'run',return_value=SimpleNamespace(returncode=0,stdout=DENIAL.encode())):
            self.assertIsNone(c._fetch_page_html(0))
        self.assertEqual(c.delivery_source_failure,'authentication_failed')


if __name__=='__main__':unittest.main()
