"""SONAR contract: real runner/parser/Fetcher, synthetic wire and memory persistence.
No site requests or database connections. Database durability is outside this test.
"""
import contextlib
import copy
import io
import json
import re
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import Fetcher, Response
from crawler.enumeration.plan import PlanError, load_plan, site_plan
from crawler.enumeration.runner import SiteRunner
from tests.enumeration_adapter_helpers import ROOT, load_crawler
from tests.enumeration_fakes import FakeTransport
from tests.sonar_memory import MemoryStore

SITE = 'sonar-ch-global'
PLAN = ROOT / 'tests/fixtures/sonar-contract-plan.json'


def record(n, day='2020-01-01', **updates):
    hit = {'id': str(n), 'created': day+'T12:34:56Z', 'metadata': {
        'pid': 'pid-'+str(n), 'documentType': 'coar:c_7a1f',
        'title': [{'mainTitle': [{'language': 'eng', 'value': 'Thesis '+str(n)}]}],
        'abstracts': [{'language': 'eng', 'value': 'Synthetic abstract. '*10}],
        'provisionActivity': [{'type': 'bf:Publication', 'startDate': '1999'}],
        'contribution': [{'role': ['cre'], 'agent': {'preferred_name': 'Author'}}],
        'organisation': [{'name': 'University'}], 'language': [{'value': 'eng'}],
        'identifiedBy': [{'type': 'bf:Doi', 'value': '10.1234/test'}],
        'subjects': [{'label': {'value': ['Test']}}],
        '_files': [{'key': 'thesis.pdf', 'links': {'download': '/files/thesis.pdf'}}],
        'partOf': [{'document': {'title': 'Series'}, 'numberingVolume': '2', 'numberingIssue': '1'}]}}
    hit.update(updates)
    return hit


class API:
    def __init__(self, records, *, ignore_filter=False, missing_total=False):
        self.records = records
        self.ignore_filter = ignore_filter
        self.missing_total = missing_total
        self.urls = []
        self.max_window = 0

    def __call__(self, url):
        self.urls.append(url)
        u = urlsplit(url)
        assert u.hostname == 'sonar.ch'
        if u.path == '/robots.txt':
            return Response(200, {}, b'User-agent: *\nAllow: /\n', url)
        assert u.path == '/api/documents/', 'detail/file requests forbidden'
        q = {k:v[0] for k,v in parse_qs(u.query, keep_blank_values=True).items()}
        assert q['document_type'] == 'coar:c_7a1f' and q['view'] == 'global'
        assert q['sort'] == 'oldest'
        size, page = int(q['size']), int(q['page'])
        self.max_window = max(self.max_window, page*size)
        if page*size > 10000:
            return Response(400, {}, b'window limit', url)
        rows = self.records
        if q.get('q') and not self.ignore_filter:
            match = re.fullmatch(r'_created:\[(\d{4}-\d{2}-\d{2}) TO (\d{4}-\d{2}-\d{2})\]', q['q'])
            assert match, q
            low, high = match.groups()
            rows = [r for r in rows if low <= r.get('created', '')[:10] <= high]
        rows = sorted(rows, key=lambda r:(r.get('created',''), str(r['id'])))
        hits = {'hits': copy.deepcopy(rows[(page-1)*size:page*size])}
        if not self.missing_total:
            hits['total'] = {'value': len(rows), 'relation': 'eq'}
        return Response(200, {'Content-Type':'application/json'}, json.dumps({'hits':hits}), url)


class Offline(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name in ('socket.socket.connect', 'socket.socket.connect_ex', 'socket.getaddrinfo',
                     'socket.create_connection', 'sqlite3.connect', 'psycopg.connect',
                     'subprocess.Popen._execute_child'):
            self.stack.enter_context(patch(name, side_effect=AssertionError('forbidden real I/O: '+name)))
        self.crawler = load_crawler(SITE)
        self.store = MemoryStore()
        self.docs = {}
        self.tmp = self.stack.enter_context(tempfile.TemporaryDirectory())

    def config(self, **over):
        data = copy.deepcopy(load_plan(PLAN))
        site = data['sites'][SITE]
        site['segments'].update({'from':'2020','to':'2021'}, **over)
        return data

    def run_plan(self, api, data=None, max_requests=20000):
        sp = site_plan(data or self.config(), SITE, registered=[SITE])
        adapter = Adapter(SITE, sp.section('adapter'), None, crawler=self.crawler)
        def save(paper):
            post, url = paper_identity(clean_paper(paper))
            if post in self.docs:
                assert paper_identity(clean_paper(self.docs[post])) == (post,url), 'identity collision'
                return list(self.docs).index(post)+1, False, None
            self.docs[post] = copy.deepcopy(paper)
            return len(self.docs), True, None
        adapter.save = save  # replace ONLY persistence: real contract materialization above
        fetcher = Fetcher(SITE, FakeTransport(api), Path(self.tmp)/'gates', interval=0,
                          max_requests=max_requests, retries=1, resolver=lambda h:['93.184.216.34'])
        def duplicates(conn, site):
            counts = Counter(paper_identity(clean_paper(p))[0] for p in self.docs.values())
            return sum(n-1 for n in counts.values() if n>1)
        with patch('crawler.enumeration.runner.duplicate_rows', duplicates), contextlib.redirect_stdout(io.StringIO()):
            return SiteRunner(sp, self.store, fetcher, adapter, today='2021-12-31').run()

    def test_plan_and_adapter(self):
        sp = site_plan(load_plan(PLAN), SITE, registered=[SITE])
        a = Adapter(SITE, sp.section('adapter'), None, crawler=self.crawler)
        self.assertFalse(a.needs_detail())
        self.assertTrue(a.identity_uses_list_fields())
        self.assertEqual(1.5, sp.get('request_interval_s'))

    def test_query_range_rejects_unsafe_or_scope_replacing_config(self):
        for fields in ({'field':'_created OR *'}, {'param':'q&scope'}, {'to_inclusive':False}):
            with self.subTest(fields=fields):
                data=self.config();data['sites'][SITE]['segments']['filter'].update(fields)
                with self.assertRaises(PlanError): site_plan(data,SITE)
        for location in ('url','params','overall'):
            data=self.config();s=data['sites'][SITE]
            if location=='url':s['listing']['url']+='?q=author'
            elif location=='params':s['listing']['params']['q']='author'
            else:s['overall_total_check']=False
            with self.assertRaises(PlanError):site_plan(data,SITE)

    def test_unknown_list_shape_or_id_rejected(self):
        for bad in ({}, [], {'hits':None}, {'hits':{'hits':'oops'}}):
            self.assertIsNone(self.crawler._parse_search_results(bad))
        for bad in (None, True, [], {}):
            hit=record(1); hit['id']=bad;hit['metadata']['pid']=bad
            with self.assertRaises(ValueError):self.crawler._parse_search_results({'hits':{'hits':[hit]}})
        self.assertEqual([],self.crawler._parse_search_results({'hits':{'hits':[]}}))

    def test_lower_bound_total_or_unknown_total_shape_rejected(self):
        for total in (5, {'value': 5, 'relation': 'gte'}, {'value': True, 'relation': 'eq'},
                      {'value': -1, 'relation': 'eq'}, {'value': 5}):
            with self.subTest(total=total), self.assertRaises(ValueError):
                self.crawler._parse_search_results({'hits':{'hits':[record(1)],'total':total}})

    def test_exact_papers_and_skip_rules_equal_unchanged_crawl(self):
        records=[record(i) for i in range(1,8)]
        records[1]['metadata'].pop('pid')
        records[2]['metadata']['permalink']='https://sonar.ch/global/documents/custom'
        records[3]['metadata']['abstracts'][0]['value']='short'
        records[4]['metadata']['title']=[]
        records[5]['metadata']['_files']=[]
        records[6].pop('created')
        saved=[];c=self.crawler;c.delivery_cursor={}
        c._fetch_page=lambda page,size:{'hits':{'hits':copy.deepcopy(records) if page==1 else []}}
        def save(p): saved.append(copy.deepcopy(p));c._last_save_created=True
        c._save_paper=save;c._advance_cursor=lambda *a,**k:None;c._mark_exhausted=lambda:None
        with patch.dict(type(c).crawl.__globals__, {'_RATE_SLEEP':0}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(5,c.crawl())
            rows=c._parse_search_results({'hits':{'hits':records}})
            papers=[c._paper_from_result(r['record']) for r in rows]
        self.assertEqual(saved,[p for p in papers if p is not None])
        self.assertEqual([paper_identity(clean_paper(p)) for p in saved],
                         [paper_identity(clean_paper(p)) for p in papers if p is not None])

    def test_split_reconcile_and_same_papers(self):
        recs=[record(i,'2020-01-%02d'%(i%20+1)) for i in range(1,80)] + [record(100,'2021-06-01')]
        api=API(recs);result=self.run_plan(api,self.config(hidden_cap=30))
        self.assertEqual('complete',result['status'],result)
        segs={r['segment_key']:r for r in self.store.data['segments'].values()}
        self.assertEqual('split',segs['2020']['status']);self.assertEqual('split',segs['2020-01']['status'])
        self.assertEqual(len(recs),sum(r['reported_total'] for r in segs.values() if r['status']!='split'))
        self.assertEqual(len(recs),self.store.site(SITE)['state']['overall_total'])
        for rec in recs:
            self.assertEqual(clean_paper(self.crawler._parse_record(rec)),self.docs[rec['metadata']['pid']])
        self.assertLessEqual(api.max_window,10000)

    def test_budget_and_serialized_resume(self):
        recs=[record(i,'2020-01-%02d'%(i%20+1)) for i in range(1,135)]
        first=self.run_plan(API(recs),max_requests=3)
        self.assertEqual(('incomplete','request_budget'),(first['status'],first['stop_reason']))
        self.assertTrue(0<len(self.docs)<len(recs))
        snapshot=json.dumps({'state':self.store.data,'docs':self.docs})
        restored=json.loads(snapshot);self.store=MemoryStore();self.store.data=restored['state'];self.docs=restored['docs']
        result=self.run_plan(API(recs))
        self.assertEqual('complete',result['status'],result)
        self.assertEqual({r['metadata']['pid'] for r in recs},set(self.docs))
        self.assertEqual(0,result['duplicates']['new'])

    def test_real_window_splits_before_deep_request(self):
        recs=[record(i,'2020-01-%02d'%(i%28+1)) for i in range(10001)]
        api=API(recs);result=self.run_plan(api)
        self.assertEqual('complete',result['status'],result)
        self.assertEqual(10001,len(self.docs));self.assertLessEqual(api.max_window,10000)

    def test_interrupt_after_saved_page_and_resume(self):
        recs=[record(i) for i in range(120)]
        class InterruptAPI(API):
            def __call__(self,url):
                if '/api/documents/' in url and len(self.urls)==2:
                    raise KeyboardInterrupt()
                return super().__call__(url)
        with self.assertRaises(KeyboardInterrupt):
            self.run_plan(InterruptAPI(recs))
        self.assertEqual('interrupted',self.store.site(SITE)['stop_reason'])
        self.assertEqual(50,len(self.docs))
        restored=json.loads(json.dumps({'state':self.store.data,'docs':self.docs}))
        self.store=MemoryStore();self.store.data=restored['state'];self.docs=restored['docs']
        result=self.run_plan(API(recs))
        self.assertEqual('complete',result['status'],result)
        self.assertEqual(120,len(self.docs))

    def test_daily_overflow_never_complete(self):
        data=self.config(hidden_cap=30)
        recs=[record(i) for i in range(80)]
        result=self.run_plan(API(recs),data)
        self.assertNotEqual('complete',result['status'],result)
        self.assertTrue(any(r['status']=='cap_unsplittable' for r in self.store.data['segments'].values()))

    def test_missing_created_is_caught_by_overall_total(self):
        result=self.run_plan(API([record(1),record(2,created='')]))
        self.assertEqual('complete_with_mismatch',result['status'],result)
        self.assertIn('overall_total',result['stop_reason'])

    def test_ignored_filter_is_not_complete(self):
        result=self.run_plan(API([record(1)],ignore_filter=True))
        self.assertNotEqual('complete',result['status'],result)

    def test_skipped_abstract_remains_review_not_silent_complete(self):
        recs=[record(1),record(2)];recs[0]['metadata']['abstracts']=[]
        result=self.run_plan(API(recs))
        self.assertEqual('complete_with_review',result['status'],result)
        self.assertEqual({'pid-2'},set(self.docs))

    def test_no_total_is_unverified(self):
        result=self.run_plan(API([record(1)],missing_total=True))
        self.assertNotEqual('complete',result['status'],result)

    def test_same_date_unstable_offset_loses_36_of_652_and_reconciliation_catches_it(self):
        # Regression from the user's NTRS incident, reproduced with synthetic SONAR JSON.
        # Date-only ties change at page 2: 36 old rows replace 36 unseen rows.
        recs=[record(i) for i in range(652)]
        class UnstableAPI(API):
            def __call__(self,url):
                response=super().__call__(url)
                if '/api/documents/' not in url:
                    return response
                q=parse_qs(urlsplit(url).query)
                data=response.json()
                if q.get('page')==['2'] and len(data['hits']['hits'])==50:
                    ordered=sorted(self.records,key=lambda r:(r['created'],str(r['id'])))
                    data['hits']['hits'][:36]=copy.deepcopy(ordered[:36])
                return Response(200,{'Content-Type':'application/json'},json.dumps(data),url)
        result=self.run_plan(UnstableAPI(recs))
        self.assertEqual(616,len(self.docs))
        self.assertEqual('complete_with_mismatch',result['status'],result)
        leaves=[r for r in self.store.data['segments'].values() if r['status']!='split']
        bad=[r for r in leaves if r['segment_key']=='2020-01-01'][0]
        self.assertEqual((616,652,'mismatch'),(bad['listed'],bad['reported_total'],bad['status']))
        self.assertIn('overall_total',result['stop_reason'])

    def test_same_date_stable_unique_id_tie_break_collects_all_652(self):
        # Fake API defines stable (created, id) order. Live SONAR support is NOT implied.
        result=self.run_plan(API([record(i) for i in range(652)]))
        self.assertEqual('complete',result['status'],result)
        self.assertEqual(652,len(self.docs))
        self.assertEqual(0,result['duplicates']['new'])
