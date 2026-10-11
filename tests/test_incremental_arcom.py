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

SITE='arcom-fr-presse'
BASE='https://www.arcom.fr'
BODY='A controlled statistical-series description for durable discovery, ordering and repeat request measurement.'
QUERY=CRAWLERS[SITE]._LIST_BASE.split('?',1)[1].replace('_ASC','_DESC')


def stamp(i):return date(2026,1,1)+timedelta(days=i)


def french_stamp(i):
    d=stamp(i);months=CRAWLERS[SITE].crawl.__globals__['_FRENCH_MONTHS']
    return f"{d.day:02} {next(k for k,v in months.items() if int(v)==d.month)} {d.year}"


def listing(ids,page):
    total=len(ids);last=(total-1)//50;current=min(page,last)
    fields='<input name="field_type_de_presse_target_id[16]" value="16" checked>'
    fields+=''.join(f'<input name="{k}" value="">' for k in ['field_date_de_decision_value_1','field_date_de_decision_value'])
    fields+='<select name="sort_bef_combine"><option value="field_date_de_decision_value_DESC">Newest</option></select>'
    cards=''.join(f'<div class="views-row"><h2 class="card-title"><a href="/presse/series-{i}">Series {i}</a></h2><div class="card-infos"><ul class="tag-list"><li>{french_stamp(i)}</li></ul></div><div class="card-footer"><ul class="tag-list"><li>Communiqué de presse</li></ul></div></div>' for i in ids[page*50:page*50+50])
    if not cards:cards='<div class="view-empty">Aucun résultat trouvé pour cette recherche, essayez une autre combinaison.</div>'
    href=lambda n:html.escape('?'+QUERY+'&page='+str(n))
    pager=f'<ul class="pagination"><li class="active"><a href="{href(current)}">Current</a></li>'
    if current<last:pager+=f'<li class="pager__item--next"><a href="{href(current+1)}">Next</a></li>'
    return f'<html><h1>Espace presse</h1><form id="views-exposed-form-presse-block-1">{fields}</form><div class="view-id-presse">{total} résultat(s) trouvé(s) sur le site.{cards}{pager}</ul></div></html>'


def detail(title,body=BODY,url=None,date_raw=None):
    if url is None:url=BASE+'/presse/series-'+title.split()[-1]
    if date_raw is None:date_raw=french_stamp(int(title.split()[-1]))
    return f'<html><head><link rel="canonical" href="{html.escape(url)}"></head><body><main><h1>{html.escape(title)}</h1><div class="field--name-field-date-de-decision">Publié le {date_raw}</div><div class="field--name-field-type-de-presse"><ul><li>Communiqué de presse</li></ul></div><div class="field--name-field-presse-contenu">{body}</div></main></body></html>'


class Transport:
    def __init__(self):self.ids=list(range(125,0,-1));self.calls=[];self.bad_empty=False;self.bad_detail=False;self.modified=False
    def __call__(self,method,url,**kwargs):
        assert method.upper()=='GET';assert kwargs['allow_redirects'] is False
        self.calls.append(url);p=urlsplit(url)
        if p.path=='/presse':
            n=int(parse_qs(p.query).get('page',['0'])[0]);raw=listing(self.ids,n)
            if self.bad_empty and n*50>=len(self.ids):raw=raw.replace('Aucun résultat trouvé pour cette recherche, essayez une autre combinaison.','Service unavailable')
        else:raw=detail('Series '+p.path.rsplit('-',1)[-1],'' if self.bad_detail else BODY+(' Revised' if self.modified and p.path.endswith('series-1') else ''))
        r=requests.Response();r.url=url;r.status_code=503 if self.bad_detail and p.path!='/presse' else 200;r._content=raw.encode();r.encoding='utf-8';return r


class ARCOMTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.conn=db_libertree.open_db(Path(self.tmp.name)/'owned.sqlite');self.addCleanup(self.conn.close)
        db_libertree.init_db(self.conn);init_schema(self.conn);db_libertree.upsert_site(self.conn,SITE,'ARCOM',BASE)
        self.fixture=Transport();self.enterContext(patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'sqlite'}));self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def instance(self):
        c=CRAWLERS[SITE](self.conn,delay=0);self.addCleanup(c._session.close);c._session.request=self.fixture
        return c,adapter_for(c)

    def collect(self,limit=None):
        c,a=self.instance();col=Collector(c,a,Policy(full_check_days=0));return col.run(limit),col

    def test_discovery_resume_and_repeat_requests(self):
        self.assertEqual(self.collect(3)[0],3);s=Store(self.conn,SITE).state();self.assertTrue(s['discovery_exhausted']);self.assertFalse(s.get('baseline_complete',False))
        self.fixture.calls.clear();self.assertEqual(self.collect()[0],122);self.assertEqual(len(self.fixture.calls),122)
        self.fixture.calls.clear();saved,col=self.collect();self.assertEqual(saved,0);self.assertEqual(len(self.fixture.calls),3)
        self.assertEqual(col.stats['detail_requests'],0);self.assertEqual(col.stats['stop_reason'],'known_boundary_plus_overlap')
        self.assertEqual([r[0] for r in self.conn.execute('SELECT post_number FROM documents ORDER BY seq_id')],['series-'+str(i) for i in range(1,126)])

    def test_new_items_stored_oldest_first(self):
        self.collect();self.fixture.ids=[127,126]+self.fixture.ids;self.assertEqual(self.collect()[0],2)
        self.assertEqual([r[0] for r in self.conn.execute('SELECT post_number FROM documents ORDER BY seq_id DESC LIMIT 2')],['series-127','series-126'])

    def test_oldest_order_is_detected(self):
        self.fixture.ids.reverse();self.collect();s=Store(self.conn,SITE).state()
        self.assertEqual(s['order'],'oldest_first');self.assertEqual(s['support'],'full_rescan_required')

    def test_unknown_empty_page_never_completes(self):
        self.fixture.bad_empty=True
        with self.assertRaisesRegex(ValueError,'explicit source end'):self.collect()
        self.assertFalse(Store(self.conn,SITE).state().get('baseline_complete',False))

    def test_missing_detail_remains_pending_then_resumes(self):
        self.fixture.bad_detail=True
        with self.assertRaises(requests.HTTPError):self.collect()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0],0)
        self.fixture.bad_detail=False;self.fixture.calls.clear();self.assertEqual(self.collect()[0],125)
        self.assertTrue(Store(self.conn,SITE).state()['baseline_complete'])

    def test_filter_drift_and_duplicate_membership_rejected(self):
        c,a=self.instance()
        with self.assertRaisesRegex(ValueError,'scope'):a.page_number(BASE+'/presse?page=1')
        self.fixture.ids[51]=self.fixture.ids[0]
        with self.assertRaisesRegex(ValueError,'identity_repeat'):self.collect()

    def test_source_drift_before_next_page_retains_no_false_baseline(self):
        c,a=self.instance();p=a.fetch(None,'source');self.fixture.ids=[126]+self.fixture.ids
        with self.assertRaisesRegex(ValueError,'drift'):a.fetch(p.next_cursor,'source')

    def test_periodic_full_check_records_modified_and_missing_without_deleting(self):
        self.collect();self.fixture.modified=True;self.fixture.ids.remove(2)
        store=Store(self.conn,SITE);s=store.state();s['last_full_at']=0;store.checkpoint(s)
        c,a=self.instance();self.assertEqual(Collector(c,a,Policy(full_check_days=1)).run(),0)
        kinds={r[0] for r in self.conn.execute('SELECT kind FROM crawl_incremental_changes')}
        self.assertIn('modified',kinds);self.assertIn('missing',kinds)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0],125)
        self.assertEqual(self.conn.execute("SELECT abstract FROM documents WHERE post_number='series-1'").fetchone()[0],BODY)

    def test_native_repeat_count_uses_actual_created_flag(self):
        c,_=self.instance();g=c.crawl.__globals__
        def curl(url,*args,**kwargs):return self.fixture('GET',url,allow_redirects=False).text
        def already_present(paper):c._last_save_created=False;return 123
        with patch.dict(g,{'_curl_get':curl}),patch.object(c,'_save_paper',already_present),patch('time.sleep'):
            self.assertEqual(c.crawl(limit=1),0)

    def test_native_failed_fetch_is_three_attempts_not_nine(self):
        from types import SimpleNamespace
        c,a=self.instance()
        with patch.object(c.crawl.__globals__['subprocess'],'run',return_value=SimpleNamespace(returncode=22,stdout=b'error')) as run,patch('time.sleep'):
            self.assertIsNone(c._fetch_detail('missing',BASE+'/presse/missing'))
            self.assertEqual(run.call_count,3)
        self.assertEqual(c.delivery_source_failure,'fetch_failed')

    def test_short_native_body_requires_full_rescan_without_false_baseline(self):
        c,a=self.instance();original=self.fixture.__call__
        def request(method,url,**kwargs):
            r=original(method,url,**kwargs)
            if '/presse/series-' in url:r._content=r.content.replace(BODY.encode(),b'short')
            return r
        c._session.request=request
        with self.assertRaisesRegex(ValueError,'short_body'):Collector(c,a,Policy(full_check_days=0)).run()
        state=Store(self.conn,SITE).state();self.assertFalse(state.get('baseline_complete',False))
        self.assertEqual(state['support'],'full_rescan_required')

    def test_wrong_detail_canonical_keeps_pending(self):
        c,a=self.instance();original=self.fixture.__call__
        def request(method,url,**kwargs):
            r=original(method,url,**kwargs)
            if '/presse/series-' in url:r._content=r.content.replace(url.encode(),b'https://example.test/wrong')
            return r
        c._session.request=request
        with self.assertRaisesRegex(ValueError,'identity'):Collector(c,a,Policy(full_check_days=0)).run()
        self.assertFalse(Store(self.conn,SITE).state().get('baseline_complete',False))

    def test_two_missing_rows_in_overlap_are_saved_without_other_details(self):
        all_ids=list(self.fixture.ids)
        self.fixture.ids=[i for i in all_ids if i not in (69,70)]
        self.collect();self.fixture.ids=all_ids;self.fixture.calls.clear()
        self.assertEqual(self.collect()[0],2)
        details=[u for u in self.fixture.calls if '/presse/series-' in u]
        self.assertEqual(details,[BASE+'/presse/series-69',BASE+'/presse/series-70'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0],125)


if __name__=='__main__':unittest.main()
