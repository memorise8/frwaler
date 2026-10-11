import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from urllib.parse import parse_qs,urlsplit,urlencode

from crawler import db_libertree
from crawler.incremental import Collector,Policy,Store,UnsupportedSource,init_schema
from crawler.incremental_acpr import ACPRAdapter
from crawler.sites import CRAWLERS
from delivery.incremental_registry import adapter_for
from tests.test_acpr_delivery_scopes import listing,detail,PATHS,QUERY,page_url


def fixtures():return {'studies':[f's{i}' for i in range(18,12,-1)],'press':[f'p{i}' for i in range(6,0,-1)]}


def response_for(url,docs,**kw):
    p=urlsplit(url);q=parse_qs(p.query,keep_blank_values=True)
    if p.path in PATHS.values():
        scope=next(s for s in PATHS if PATHS[s]==p.path);page=int(q['page'][0]);raw=listing(scope,page,ids=docs[scope],size=3)
        for uid in docs[scope]:
            old=f'<h3 class="card-title">Research {uid}</h3><span class="category-btn-grid">Publication</span><small>14 Septembre 2026'
            raw=raw.replace(old,old.replace('14 Septembre 2026',f'{int(uid[1:])} Septembre 2026'))
        if q.get('start-date_year',[''])[0]:
            for n in range((len(docs[scope])+2)//3):
                raw=raw.replace(page_url(scope,n),url.split('?')[0]+'?'+urlencode({k:(str(n) if k=='page' else v[0]) for k,v in q.items()}))
        return Mock(text=raw)
    uid=p.path.rsplit('/',1)[-1];return Mock(text=detail(uid,date=f'{int(uid[1:]):02}/09/2026'))


class ACPRIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.conn=db_libertree.open_db(Path(self.tmp.name)/'owned.sqlite');self.addCleanup(self.conn.close)
        db_libertree.init_db(self.conn);init_schema(self.conn)
        self.cls=CRAWLERS['acpr-banque-france-fr-fr'];db_libertree.upsert_site(self.conn,self.cls.site_id,'ACPR',self.cls.base_url)
        self.enterContext(patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'sqlite'}));self.enterContext(patch.object(ACPRAdapter,'rows',3))
        self.docs=fixtures();self.calls=[];self.policy=Policy(full_check_days=0)

    def instance(self):
        c=self.cls(self.conn,delay=0)
        def request(url,**kw):self.calls.append(url);return response_for(url,self.docs,**kw)
        c._request=request;return c,adapter_for(c)

    def run_collector(self,limit=None):
        c,a=self.instance();col=Collector(c,a,self.policy);return col.run(limit),col

    def test_full_merge_then_repeat_preserves_raw_dates_and_original_filters(self):
        self.assertEqual(self.run_collector()[0],12)
        rows=list(self.conn.execute('SELECT post_number,published_date,published_date_raw,listed_date_raw FROM documents ORDER BY seq_id'))
        self.assertEqual([r[0] for r in rows],[f'p{i}' for i in range(1,7)]+[f's{i}' for i in range(13,19)])
        self.assertEqual(rows[0][1:],('2026-09-01','01/09/2026','1 Septembre 2026'))
        for url in self.calls:
            if '/etudes-et-recherche?' in url:
                q=parse_qs(urlsplit(url).query,keep_blank_values=True);q.pop('page');self.assertEqual(q,{k:[v] for k,v in QUERY.items()})
        self.calls=[];saved,col=self.run_collector();self.assertEqual(saved,0);self.assertEqual(len(self.calls),6)
        self.assertEqual(col.stats['detail_requests'],0);self.assertEqual(col.stats['stop_reason'],'known_boundary_plus_overlap')

    def test_partial_save_resumes_remaining_details_only(self):
        self.assertEqual(self.run_collector(3)[0],3);self.assertFalse(Store(self.conn,self.cls.site_id).state().get('baseline_complete',False))
        self.calls=[];self.assertEqual(self.run_collector()[0],9);self.assertEqual(len(self.calls),9)
        self.assertTrue(all('?' not in u for u in self.calls))

    def test_new_item_in_older_press_scope_not_hidden(self):
        self.run_collector();self.docs['press'].insert(0,'p7');self.calls=[]
        self.assertEqual(self.run_collector()[0],1)
        self.assertEqual(sum('?' not in u for u in self.calls),1)

    def test_bounded_years_include_months_and_preserve_format_filters(self):
        c,_=self.instance();a=ACPRAdapter(c,scopes=['studies'],years=(2025,2026))
        self.assertEqual(Collector(c,a,self.policy).run(),6)
        q=parse_qs(urlsplit(self.calls[0]).query,keep_blank_values=True)
        self.assertEqual(q['start-date_month'],['01']);self.assertEqual(q['end-date_month'],['12'])
        self.assertEqual(q['format[5412591]'],['5412591']);self.assertEqual(q['format[5412600]'],['5412600'])

    def test_optional_override_cannot_remove_or_change_delivered_filters(self):
        c,a=self.instance();helper=c.crawl.__globals__['_scope_query']
        for query in [{},dict(QUERY,**{'format[5412600]':'999'}),dict(QUERY,search_publications_api_fulltext='unrelated')]:
            with self.assertRaises(ValueError):helper('studies',query)

    def test_ignored_year_filter_is_not_bounded_completion(self):
        c,_=self.instance();a=ACPRAdapter(c,scopes=['studies'],years=(2024,2025))
        with self.assertRaisesRegex(ValueError,'ignored requested date bounds'):Collector(c,a,self.policy).run()

    def test_partial_card_date_is_explicitly_unsupported(self):
        c,a=self.instance();native=c._request
        c._request=lambda url,**kw:Mock(text=native(url,**kw).text.replace('18 Septembre 2026','Septembre 2026'))
        with self.assertRaises(UnsupportedSource):Collector(c,a,self.policy).run()

    def test_nonchronological_source_requires_review(self):
        self.docs['studies'].reverse()
        with self.assertRaises(UnsupportedSource):self.run_collector()

    def test_cross_scope_slug_collision_is_not_silently_merged(self):
        self.docs['press'][0]='s18'
        with self.assertRaisesRegex(UnsupportedSource,'slug_collision'):self.run_collector()

    def test_anchor_change_rejects_resume(self):
        c,a=self.instance();Collector(c,a,Policy(max_pages=1,full_check_days=0)).run();self.docs['studies'][0]='s19'
        with self.assertRaisesRegex(ValueError,'previous page changed'):self.run_collector()

    def test_unavailable_html_and_pdf_body_keeps_pending(self):
        c,a=self.instance();native=c._request;c._abstract_from_pdf=Mock(side_effect=ValueError('source PDF text unavailable'))
        c._request=lambda url,**kw:Mock(text=detail('p1',body='') if '?' not in url else native(url,**kw).text)
        with self.assertRaisesRegex(ValueError,'PDF text unavailable'):Collector(c,a,self.policy).run()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM crawl_incremental_pending').fetchone()[0],12)
        self.assertFalse(Store(self.conn,c.site_id).state().get('baseline_complete',False))

    def test_full_recheck_revisits_details_without_changing_existing_rows(self):
        self.run_collector();before=list(self.conn.execute('SELECT * FROM documents ORDER BY seq_id'));self.calls=[]
        c,a=self.instance();c.delivery_mode='full';self.assertEqual(Collector(c,a,self.policy).run(),0)
        self.assertEqual(sum('?' not in u for u in self.calls),12)
        self.assertEqual(list(self.conn.execute('SELECT * FROM documents ORDER BY seq_id')),before)

if __name__=='__main__':unittest.main()
