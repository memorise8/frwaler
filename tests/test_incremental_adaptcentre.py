import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests
from crawler import db_libertree
from crawler.incremental import Collector, Policy, Store, UnsupportedSource, init_schema
from crawler.incremental_adaptcentre import AdaptCentreAdapter, ENDPOINT
from crawler.sites import CRAWLERS
from delivery.incremental_registry import adapter_for

SITE='adaptcentre-ie-research'
ABSTRACT='Controlled abstract response for testing incremental transport and native metadata persistence only. '
END={'code':'rest_post_invalid_page_number','data':{'status':400}}


def record(i):
    return dict(id=1000+i,slug=f'paper-{i}',link=f'https://www.adaptcentre.ie/publications/paper-{i}/',
        date=f'2026-09-{i:02d}T12:00:00',title={'rendered':f'Research {i}'},journal_type=[1],
        acf=dict(date='20200101',authors_list='A, B',name_of_publication='Controlled Journal',
                 type='Journal Articles',url=f'https://doi.org/10.1234/controlled-{i}'))


def response(url,data,status=200,headers=None):
    r=requests.Response();r.url=url;r.status_code=status;r.headers.update(headers or {})
    r._content=json.dumps(data).encode();r.encoding='utf-8';return r


class Transport:
    def __init__(self,docs,rows=3):
        self.docs=docs;self.rows=rows;self.calls=[];self.end=END;self.total_delta=0;self.no_abstract=False

    def __call__(self,method,url,**kwargs):
        assert method.lower()=='get';assert kwargs['allow_redirects'] is False
        assert kwargs['headers']['Accept-Encoding']=='gzip, deflate'
        p=urlsplit(url);self.calls.append(url)
        if url.startswith(ENDPOINT):
            page=int(parse_qs(p.query)['page'][0]);total=len(self.docs)+self.total_delta
            if page>(len(self.docs)+self.rows-1)//self.rows:return response(url,self.end,400)
            return response(url,self.docs[(page-1)*self.rows:page*self.rows],headers={
                'X-WP-Total':str(total),'X-WP-TotalPages':str((total+self.rows-1)//self.rows)})
        if p.netloc=='api.openalex.org':
            index={} if self.no_abstract else {word:[i] for i,word in enumerate(ABSTRACT.split())}
            return response(url,{'abstract_inverted_index':index,'results':[{'abstract_inverted_index':index}]})
        if p.netloc=='api.crossref.org':
            return response(url,{'message':{'items':[] if self.no_abstract else [{'abstract':ABSTRACT}]}})
        raise AssertionError('unreviewed HTTP request')


class AdaptCentreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.conn=db_libertree.open_db(Path(self.tmp.name)/'owned.sqlite');self.addCleanup(self.conn.close)
        db_libertree.init_db(self.conn);init_schema(self.conn)
        db_libertree.upsert_site(self.conn,SITE,'ADAPT','https://www.adaptcentre.ie')
        self.cls=CRAWLERS[SITE];self.fixture=Transport([record(i) for i in range(12,0,-1)])
        self.enterContext(patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'sqlite'}))
        self.enterContext(patch.object(AdaptCentreAdapter,'rows',3));self.enterContext(patch('time.sleep'))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def instance(self):
        c=self.cls(self.conn,delay=0);self.addCleanup(c._session.close)
        c._session.request=self.fixture
        return c,adapter_for(c)

    def collect(self,limit=None):
        c,a=self.instance();col=Collector(c,a,Policy(full_check_days=0));return col.run(limit),col

    def test_resume_oldest_first_then_repeat_without_external_abstract_queries(self):
        self.assertEqual(self.collect(3)[0],3)
        state=Store(self.conn,SITE).state();self.assertTrue(state['discovery_exhausted']);self.assertFalse(state.get('baseline_complete',False))
        self.assertEqual(len(self.fixture.calls),11) # seven list reads + explicit end + three abstracts
        self.fixture.calls.clear();self.assertEqual(self.collect()[0],9)
        self.assertEqual(len(self.fixture.calls),9)
        self.assertTrue(all('/wp-json/' not in u for u in self.fixture.calls))
        self.assertTrue(Store(self.conn,SITE).state().get('baseline_complete',False))
        ids=[r[0] for r in self.conn.execute('SELECT post_number FROM documents ORDER BY seq_id')]
        self.assertEqual(ids,[str(1000+i) for i in range(1,13)])
        self.fixture.calls.clear();saved,col=self.collect();self.assertEqual(saved,0)
        self.assertEqual(len(self.fixture.calls),3);self.assertEqual(col.stats['detail_requests'],0)
        self.assertEqual(col.stats['stop_reason'],'known_boundary_plus_overlap')

    def test_new_records_oldest_first_and_native_publication_dates_unchanged(self):
        self.collect();self.fixture.docs=[record(15),record(14),record(13)]+self.fixture.docs
        self.fixture.calls.clear();self.assertEqual(self.collect()[0],3)
        rows=list(self.conn.execute('SELECT post_number,published_date,listed_date FROM documents ORDER BY seq_id DESC LIMIT 3'))
        self.assertEqual([r[0] for r in rows],['1015','1014','1013'])
        self.assertTrue(all(r[1]=='2020-01-01' for r in rows))
        self.assertEqual(rows[0][2],'2026-09-15')

    def test_non_page_error_cannot_certify_source_end(self):
        self.fixture.end={'code':'rest_forbidden','data':{'status':403}}
        with self.assertRaisesRegex(ValueError,'not source exhaustion'):self.collect()
        self.assertFalse(Store(self.conn,SITE).state().get('baseline_complete',False))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0],0)

    def test_short_page_is_not_implicit_exhaustion(self):
        self.fixture.docs.pop();self.fixture.total_delta=1
        with self.assertRaisesRegex(ValueError,'truncated'):self.collect()
        self.assertFalse(Store(self.conn,SITE).state().get('baseline_complete',False))

    def test_date_order_not_numeric_id_or_publication_day_is_proven(self):
        for n,item in enumerate(self.fixture.docs):item['id']=1000+(n*7)%13
        self.collect();s=Store(self.conn,SITE).state()
        self.assertEqual(s['order'],'newest_first');self.assertEqual(s['order_evidence'],'date')

    def test_missing_external_abstract_preserves_pending_and_incomplete_baseline(self):
        self.fixture.no_abstract=True
        with self.assertRaisesRegex(ValueError,'pending retained'):self.collect()
        s=Store(self.conn,SITE).state();self.assertTrue(s['discovery_exhausted']);self.assertFalse(s.get('baseline_complete',False))
        self.fixture.no_abstract=False;self.fixture.calls.clear();self.assertEqual(self.collect()[0],12)
        self.assertTrue(all('/wp-json/' not in u for u in self.fixture.calls))

    def test_repeated_cross_page_id_fails_closed(self):
        self.fixture.docs[3]['id']=self.fixture.docs[0]['id']
        with self.assertRaises(UnsupportedSource):self.collect()
        self.assertFalse(Store(self.conn,SITE).state().get('baseline_complete',False))

    def test_oldest_first_cannot_reuse_page_one_as_newest(self):
        self.fixture.docs.reverse();self.collect();s=Store(self.conn,SITE).state()
        self.assertEqual(s['order'],'oldest_first');self.assertEqual(s['support'],'full_rescan_required')


if __name__=='__main__':unittest.main()
