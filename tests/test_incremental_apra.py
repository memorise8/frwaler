import contextlib
from datetime import date,timedelta
import html
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit

import requests
from crawler import db_libertree
from crawler.incremental import Collector,Policy,Store,init_schema
from crawler.sites import CRAWLERS
from delivery.incremental_registry import adapter_for

SITE='apra-gov-au-statistics'
BASE='https://www.apra.gov.au'
BODY='A controlled statistical-series description for durable discovery, ordering and repeat request measurement.'
QUERY='created=All&document_type%5B0%5D=bundle%3Astatistical_publication&sort_bef_combine=created_DESC'


def stamp(i):return date(2026,1,1)+timedelta(days=i)


def listing(ids,page):
    total=len(ids);last=(total-1)//10;current=min(page,last)
    fields=''.join('<select name="'+k+'">'+('' if not v else '<option value="'+v+'" selected>Fixture</option>')+'</select>'
                   for k,v in [('document_type[]','bundle:statistical_publication'),('created','All'),('sort_bef_combine','created_DESC'),('field_industry_target_id[]','')])
    cards=''.join(f'<div class="views-row"><h2 class="search-result__title"><a href="/news-and-publications/series-{i}">Series {i}</a></h2><span class="anx-pill--split"><span class="anx-pill__label-first">Published</span><span class="anx-pill__label-last">{stamp(i).strftime("%d %B %Y")}</span></span><span class="tag-bungle-type">Statistical Publication</span></div>' for i in ids[page*10:page*10+10])
    if not cards:cards='<p>No items match your filters</p>'
    href=lambda n:html.escape('?'+QUERY+'&page='+str(n))
    pager=f'<nav class="pager"><a aria-current="page" href="{href(current)}">Current</a>'
    if current<last:pager+=f'<span class="pager__item--next"><a href="{href(current+1)}">Next</a></span>'
    return f'<html><h1>News and Publications</h1><form id="views-exposed-form-news-and-publications-page-1">{fields}</form><div class="search-page">{total} results Showing {current*10+1} - {min((current+1)*10,total)} results{cards}{pager}</nav></div></html>'


def detail(title,body=BODY):return f'<html><main><h1>{html.escape(title)}</h1><p>{body}</p></main></html>'


class Transport:
    def __init__(self):self.ids=list(range(25,0,-1));self.calls=[];self.bad_empty=False;self.bad_detail=False;self.modified=False
    def __call__(self,method,url,**kwargs):
        assert method.upper()=='GET';assert kwargs['allow_redirects'] is False
        self.calls.append(url);p=urlsplit(url)
        if p.path=='/news-and-publications':
            n=int(parse_qs(p.query).get('page',['0'])[0]);raw=listing(self.ids,n)
            if self.bad_empty and n*10>=len(self.ids):raw=raw.replace('No items match your filters','Service unavailable')
        else:raw=detail('Series '+p.path.rsplit('-',1)[-1],'' if self.bad_detail else BODY+(' Revised' if self.modified and p.path.endswith('series-1') else ''))
        r=requests.Response();r.url=url;r.status_code=200;r._content=raw.encode();r.encoding='utf-8';return r


class APRATests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.conn=db_libertree.open_db(Path(self.tmp.name)/'owned.sqlite');self.addCleanup(self.conn.close)
        db_libertree.init_db(self.conn);init_schema(self.conn);db_libertree.upsert_site(self.conn,SITE,'APRA',BASE)
        self.fixture=Transport();self.enterContext(patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'sqlite'}));self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def instance(self):
        c=CRAWLERS[SITE](self.conn,delay=0);self.addCleanup(c._session.close);c._session.request=self.fixture
        return c,adapter_for(c)

    def collect(self,limit=None):
        c,a=self.instance();col=Collector(c,a,Policy(full_check_days=0));return col.run(limit),col

    def test_discovery_resume_and_repeat_requests(self):
        self.assertEqual(self.collect(3)[0],3);s=Store(self.conn,SITE).state();self.assertTrue(s['discovery_exhausted']);self.assertFalse(s.get('baseline_complete',False))
        self.fixture.calls.clear();self.assertEqual(self.collect()[0],22);self.assertEqual(len(self.fixture.calls),22)
        self.fixture.calls.clear();saved,col=self.collect();self.assertEqual(saved,0);self.assertEqual(len(self.fixture.calls),3)
        self.assertEqual(col.stats['detail_requests'],0);self.assertEqual(col.stats['stop_reason'],'known_boundary_plus_overlap')
        self.assertEqual([r[0] for r in self.conn.execute('SELECT post_number FROM documents ORDER BY seq_id')],['series-'+str(i) for i in range(1,26)])

    def test_new_items_stored_oldest_first(self):
        self.collect();self.fixture.ids=[27,26]+self.fixture.ids;self.assertEqual(self.collect()[0],2)
        self.assertEqual([r[0] for r in self.conn.execute('SELECT post_number FROM documents ORDER BY seq_id DESC LIMIT 2')],['series-27','series-26'])

    def test_oldest_order_is_detected(self):
        self.fixture.ids.reverse();self.collect();s=Store(self.conn,SITE).state()
        self.assertEqual(s['order'],'oldest_first');self.assertEqual(s['support'],'full_rescan_required')

    def test_unknown_empty_page_never_completes(self):
        self.fixture.bad_empty=True
        with self.assertRaisesRegex(ValueError,'empty page'):self.collect()
        self.assertFalse(Store(self.conn,SITE).state().get('baseline_complete',False))

    def test_missing_detail_remains_pending_then_resumes(self):
        self.fixture.bad_detail=True
        with self.assertRaisesRegex(ValueError,'pending retained'):self.collect()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0],0)
        self.fixture.bad_detail=False;self.fixture.calls.clear();self.assertEqual(self.collect()[0],25)
        self.assertTrue(all('/series-' in u for u in self.fixture.calls))

    def test_filter_drift_and_duplicate_membership_rejected(self):
        c,a=self.instance()
        with self.assertRaisesRegex(ValueError,'scope'):a.page_number(BASE+'/news-and-publications?created=All&page=1')
        self.fixture.ids[11]=self.fixture.ids[0]
        with self.assertRaisesRegex(ValueError,'membership'):self.collect()

    def test_source_drift_before_next_page_retains_no_false_baseline(self):
        c,a=self.instance();p=a.fetch(None,'source');self.fixture.ids=[26]+self.fixture.ids
        with self.assertRaisesRegex(ValueError,'changed'):a.fetch(p.next_cursor,'source')

    def test_periodic_full_check_records_modified_and_missing_without_deleting(self):
        self.collect();self.fixture.modified=True;self.fixture.ids.remove(2)
        store=Store(self.conn,SITE);s=store.state();s['last_full_at']=0;store.checkpoint(s)
        c,a=self.instance();self.assertEqual(Collector(c,a,Policy(full_check_days=1)).run(),0)
        kinds={r[0] for r in self.conn.execute('SELECT kind FROM crawl_incremental_changes')}
        self.assertIn('modified',kinds);self.assertIn('missing',kinds)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0],25)
        self.assertEqual(self.conn.execute("SELECT abstract FROM documents WHERE post_number='series-1'").fetchone()[0],BODY)

    def test_native_repeat_count_uses_actual_created_flag(self):
        c,_=self.instance();g=c.crawl.__globals__
        def curl(url,*args,**kwargs):return self.fixture('GET',url,allow_redirects=False).text
        def already_present(paper):c._last_save_created=False;return 123
        with patch.dict(g,{'_curl':curl}),patch.object(c,'_save_paper',already_present),patch('time.sleep'):
            self.assertEqual(c.crawl(limit=1),0)


if __name__=='__main__':unittest.main()
