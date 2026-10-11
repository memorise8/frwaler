import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlencode,urlsplit

import requests
from crawler import db_libertree
from crawler.incremental import Collector,Policy,Store,UnsupportedSource,init_schema
from crawler.incremental_afp_news import AFPNewsAdapter,BASE,ENDPOINT
from crawler.sites import CRAWLERS
from delivery.incremental_registry import adapter_for

SITE='afp-gov-au-news-centre'
BODY='Controlled substantive research and news article text for storage and resume tests. '*3


def listing(ids,page,bounds=None,*,hide_next=False):
    bounds=bounds or {};cards=''
    for i in ids[page*3:page*3+3]:
        cards+=f'<div class="views-row"><div data-history-node-id="{1000+i}" class="node--type-article"><div class="card--link"><a href="/news-centre/media-release/item-{i}"><span class="card--title">Research {i}</span></a></div><div class="card--date"><time datetime="2026-09-{i:02}T12:00:00+10:00">{i} September 2026</time></div></div></div>'
    filters='<div class="view-filters"><form action="/news-centre">'+''.join(f'<input name="{k}" value="{bounds.get(k,"")}">' for k in ('date_from','date_to','title'))+'<input name="field_article_type_target_id" value="All" checked><select name="sort_by"><option value="created" selected>Most recent</option></select></form></div>'
    content='<div class="view-content">'+cards+'</div>' if cards else ''
    if (page+1)*3<len(ids) and not hide_next:
        query=urlencode({**bounds,'page':page+1});content+=f'<ul data-drupal-views-infinite-scroll-pager><a rel="next" href="?{query}">Show more</a></ul>'
    return '<html><h1>News Centre</h1><div class="view-solr-news-centre view-display-id-all_news_page"><div class="view-header"><h2>News Centre</h2></div>'+filters+content+'</div></html>'


def detail(i,*,node=None,body=BODY):
    return f'<html><main id="block-system-main-block"><article data-history-node-id="{node or 1000+i}" class="node--type-article"><h1 class="article-page__title">Research {i}</h1><div class="article-page__date"><time datetime="2026-09-{i:02}T12:00:00+10:00"></time></div><div class="article-page__body"><p>{body}</p></div></article></main></html>'


class Transport:
    def __init__(self):self.ids=list(range(12,0,-1));self.calls=[];self.hide_next=False;self.bad_identity=False;self.bad_empty=False
    def __call__(self,method,url,**kwargs):
        assert method.upper()=='GET';assert kwargs['allow_redirects'] is False
        self.calls.append(url);p=urlsplit(url);q=parse_qs(p.query)
        if p.path=='/news-centre':
            page=int(q.get('page',['0'])[0]);bounds={k:q[k][0] for k in ('date_from','date_to') if k in q}
            raw=listing(self.ids,page,bounds,hide_next=self.hide_next)
            if self.bad_empty and page*3>=len(self.ids):raw=raw.replace('</form>','</form><p>Unexpected service state</p>')
        else:
            i=int(p.path.rsplit('-',1)[-1]);raw=detail(i,node=99999 if self.bad_identity else None)
        r=requests.Response();r.url=url;r.status_code=200;r._content=raw.encode();r.encoding='utf-8';return r


class AFPNewsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.conn=db_libertree.open_db(Path(self.tmp.name)/'owned.sqlite');self.addCleanup(self.conn.close)
        db_libertree.init_db(self.conn);init_schema(self.conn);db_libertree.upsert_site(self.conn,SITE,'AFP',BASE)
        self.cls=CRAWLERS[SITE];self.fixture=Transport()
        self.enterContext(patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'sqlite'}))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def instance(self):
        c=self.cls(self.conn,delay=0);self.addCleanup(c._session.close);c._session.request=self.fixture
        return c,adapter_for(c)

    def collect(self,limit=None):
        c,a=self.instance();col=Collector(c,a,Policy(full_check_days=0));return col.run(limit),col

    def test_full_discovery_resume_and_repeat(self):
        self.assertEqual(self.collect(3)[0],3);s=Store(self.conn,SITE).state()
        self.assertTrue(s['discovery_exhausted']);self.assertFalse(s.get('baseline_complete',False))
        self.assertEqual(len(self.fixture.calls),11)
        self.fixture.calls.clear();self.assertEqual(self.collect()[0],9);self.assertEqual(len(self.fixture.calls),9)
        self.assertTrue(all('/media-release/' in u for u in self.fixture.calls))
        rows=list(self.conn.execute('SELECT post_number FROM documents ORDER BY seq_id'))
        self.assertEqual([r[0] for r in rows],[str(1000+i) for i in range(1,13)])
        self.fixture.calls.clear();saved,col=self.collect();self.assertEqual(saved,0)
        self.assertEqual(len(self.fixture.calls),3);self.assertEqual(col.stats['detail_requests'],0)
        self.assertEqual(col.stats['stop_reason'],'known_boundary_plus_overlap')

    def test_new_items_delivered_oldest_first(self):
        self.collect();self.fixture.ids=[15,14,13]+self.fixture.ids
        self.assertEqual(self.collect()[0],3)
        self.assertEqual([r[0] for r in self.conn.execute('SELECT post_number FROM documents ORDER BY seq_id DESC LIMIT 3')],['1015','1014','1013'])

    def test_premature_next_absence_is_not_end(self):
        self.fixture.hide_next=True
        with self.assertRaisesRegex(ValueError,'contradicted'):self.collect()
        self.assertFalse(Store(self.conn,SITE).state().get('baseline_complete',False))

    def test_filter_echo_and_scope_changing_next_rejected(self):
        c,a=self.instance()
        with self.assertRaisesRegex(ValueError,'filter'):a.next_page(ENDPOINT+'?page=1&field_article_type_target_id=12',0)
        raw=listing([1],0).replace('value="All" checked','value="12" checked')
        with patch.object(a,'response') as get:
            get.return_value.text=raw
            with self.assertRaisesRegex(ValueError,'all-news'):a.read(0)

    def test_detail_ID_collision_does_not_save_a_related_article(self):
        self.fixture.bad_identity=True
        with self.assertRaises(UnsupportedSource):self.collect()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0],0)

    def test_unknown_empty_state_rejected(self):
        self.fixture.bad_empty=True
        with self.assertRaisesRegex(ValueError,'source-end'):self.collect()

    def test_oldest_first_is_detected_without_assuming_latest(self):
        self.fixture.ids.reverse();self.collect();s=Store(self.conn,SITE).state()
        self.assertEqual(s['order'],'oldest_first');self.assertEqual(s['support'],'full_rescan_required')

    def test_scope_changes_do_not_reuse_existing_baseline(self):
        self.collect();c,_=self.instance()
        with self.assertRaisesRegex(ValueError,'contract changed'):
            Collector(c,AFPNewsAdapter(c,date_from='2026-09-01',date_to='2026-09-30')).run()


if __name__=='__main__':unittest.main()
