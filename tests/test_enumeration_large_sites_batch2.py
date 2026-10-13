import ast
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit

from crawler.enumeration.adapters import Adapter,clean_paper,paper_identity
from crawler.enumeration.contracts import ContractError
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import (AccessBlocked,BudgetExhausted,Fetcher,PermanentFailure,PolicyRefusal,
                                      PrivateDestination,RateLimited,Response,RobotsDisallowed,RequestsTransport)
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import PlanError,site_plan
from crawler.enumeration.urls import build_url
from tests.large_sites_batch2_helpers import ROOT,SITES,TEXT,Transport,fixture,load,raw_plan,run


class NoNetwork(unittest.TestCase):
    def setUp(self):
        self.attempts=[]
        def denied(*a,**kw):self.attempts.append(True);raise AssertionError('real I/O forbidden')
        for name in ['socket.getaddrinfo','socket.socket.connect','socket.socket.connect_ex','subprocess.Popen._execute_child']:
            p=patch(name,denied);p.start();self.addCleanup(p.stop)
        self.addCleanup(lambda:self.assertEqual([],self.attempts))


class SiteContracts(NoNetwork):
    def pipeline(self,s,f=None,fallback=False):
        c,_=load(s);p=raw_plan(s);a=Adapter(s,p['adapter'],None,crawler=c);t=Transport(s,f,fallback=fallback);f=f or fixture(s)
        url=build_url(p['listing']['url'],p['listing']['params']);resp=Response(200,{'Content-Type':'application/json'},json.dumps(f['listing']),url)
        items=ListingParser(p['listing']['parser'],adapter=a).parse(resp).items
        with tempfile.TemporaryDirectory() as temp:
            fetcher=Fetcher(s,t,Path(temp),interval=0,retries=1)
            papers=[a.materialize(i,fetcher) for i in items]
        return papers,t

    def test_all_three_plans_validate_without_requests(self):
        for s in SITES:
            with self.subTest(s=s):site_plan({'sites':{s:raw_plan(s)}},s,registered=[s]);self.assertTrue(raw_plan(s)['require_reported_total'])

    def test_native_item_ids_and_mask_precision(self):
        for s,ids,dates in [(SITES[0],['000101','000102'],['2025-06-13']*2),(SITES[1],['OTS_20250613_OTS001','OTS_20250613_OTS002'],['2025-06-13']*2),(SITES[2],['101','102'],['2025-02']*2)]:
            with self.subTest(s=s):
                papers,_=self.pipeline(s);self.assertEqual(ids,[p['external_id'] for p in papers]);self.assertEqual(dates,[p['published_date'] for p in papers]);self.assertEqual(ids,[paper_identity(p)[0] for p in papers])

    def test_estat_pure_paper_matches_registered_head_name_and_all_fields(self):
        s=SITES[0];c,_=load(s);f=fixture(s);items=c._enumeration_items(f['listing'],1);head=Response(200,{'Content-Disposition':"attachment; filename*=UTF-8''report%20source.pdf"},b'');
        c._curl=lambda *a,**kw:'Content-Disposition: '+head.header('content-disposition')
        for i in items:
            detail=c._parse_detail(i['url'],f['details'][i['url']]);expected=c._build_record(i,detail)
            with guarded():actual=c._enumeration_paper(i,detail,head)
            self.assertEqual(expected,actual);self.assertEqual('report source.pdf',actual['original_filename'])

    def test_estat_survey_month_never_invents_day(self):
        c,_=load(SITES[0]);self.assertEqual('2025-02',c._survey_month_date('2025年2月'));self.assertIsNone(c._survey_month_date('unknown'))
        f=fixture(SITES[0]);rows=c._enumeration_items(f['listing'],1)
        for row in rows:
            row['listed_date']=None;row['listed_date_raw']=''
            detail=c._parse_detail(row['url'],f['details'][row['url']]);detail['fields'].pop('公開年月日時分')
            with guarded():p=c._enumeration_paper(row,detail,None)
            self.assertEqual('2025-02',p['published_date']);self.assertEqual('2025年2月',json.loads(p['metadata'])['survey_month_raw'])

    def test_estat_no_pdf_no_followup_and_header_fallback_order(self):
        c,_=load(SITES[0])
        with guarded():
            self.assertIsNone(c._enumeration_followup({'downloads':[]},{'downloads':[]}))
            self.assertEqual(('https://www.e-stat.go.jp/report.pdf','report.pdf'),c._enumeration_pdf_info([{'file_type':'PDF','url':'https://www.e-stat.go.jp/report.pdf'}],None))
            self.assertEqual(('https://www.e-stat.go.jp/d','001.pdf'),c._enumeration_pdf_info([{'file_type':'PDF','url':'https://www.e-stat.go.jp/d','statInfId':'001'}],None))

    def test_estat_html_head_is_not_pdf_metadata_success(self):
        c,_=load(SITES[0])
        with self.assertRaises(PermanentFailure):c._enumeration_pdf_info([{'url':'https://x.test/f','file_type':'PDF'}],Response(200,{'Content-Type':'text/html'}))

    def test_etera_paper_equals_original_converter_including_raw_mask(self):
        s=SITES[2];c,_=load(s);f=fixture(s)
        for row in c._enumeration_items(f['listing']):
            meta=f['details'][row['url']]
            with guarded():self.assertEqual(c._paper_from_item(row['record'],meta),c._enumeration_paper(row,meta))
        for mask,value,result in [('YYYY','20250000000000','2025'),('YYYYMM','20250200000000','2025-02'),('YYYYMMDD','20250213000000','2025-02-13')]:
            self.assertEqual(result,c._date_from_raw(value,mask))

    def test_ots_html_fallback_keeps_compound_id_and_native_date(self):
        papers,t=self.pipeline(SITES[1],fallback=True)
        self.assertEqual(['OTS_20250613_OTS001','OTS_20250613_OTS002'],[p['post_number'] for p in papers]);self.assertTrue(all(p['published_date']=='2025-06-13' for p in papers));self.assertEqual(2,len([x for x in t.calls if '/presseaussendung/' in x[1]]))

    def test_malformed_listing_or_missing_identifier_never_becomes_empty(self):
        for s in SITES:
            c,_=load(s)
            with self.subTest(s=s),self.assertRaises(PermanentFailure):c._enumeration_items({'error':'blocked'},*([1] if s==SITES[0] else []))
        for val in [None,True,{},'']:
            d=fixture(SITES[2])['listing'];d['results']['docs'][0]['obj_id']=val
            with self.subTest(val=val),self.assertRaises(PermanentFailure):load(SITES[2])[0]._enumeration_items(d)

    def test_valid_empty_and_metadata_error_distinct(self):
        for s in SITES:
            c,_=load(s);self.assertEqual([],c._enumeration_items(fixture(s)['empty'],*([1] if s==SITES[0] else [])))
        c,_=load(SITES[2]);
        with self.assertRaises(PermanentFailure):c._enumeration_detail({'error':{'message':'no access'}},{})

    def test_template_input_and_date_mask_are_not_mutated(self):
        for s in SITES:
            f=fixture(s);before=copy.deepcopy(f);self.pipeline(s,f);self.assertEqual(before,f)

    def test_real_runner_etera_post_and_ots_get_complete_synthetically(self):
        for s in SITES[1:]:
            with self.subTest(s=s):r,docs,t=run(s);self.assertEqual('complete',r['status'],r);self.assertEqual(2,len(docs));self.assertEqual(2,len({x['post_number'] for x in docs}))

    def test_estat_without_verified_total_stays_unverified(self):
        r,docs,t=run(SITES[0]);self.assertEqual(('incomplete','reported_total_missing'),(r['status'],r['stop_reason']));self.assertEqual(2,len(docs))

    def test_reported_total_mismatch_is_caught_for_both_apis(self):
        for s in SITES[1:]:
            with self.subTest(s=s):r,docs,t=run(s,Transport(s,total=3));self.assertEqual('complete_with_mismatch',r['status'],r)

    def test_post_listing_budgets_do_not_mark_complete(self):
        r,docs,t=run(SITES[2],max_requests=3);self.assertEqual('incomplete',r['status'],r)

    def test_auxiliary_403_stops_without_fallback_or_save(self):
        for s in SITES[:2]:
            t=Transport(s,fallback=s==SITES[1],deny=lambda m,u:m=='HEAD' or '/presseaussendung/' in u)
            with self.subTest(s=s):r,docs,t=run(s,t);self.assertEqual('access_blocked',r['status'],r);self.assertEqual([],docs)

    def test_bad_post_plan_and_followup_schema_rejected(self):
        p=raw_plan(SITES[2])
        for change in [{'method':'DELETE'},{'read_only':False},{'page_field':'missing'}]:
            bad=copy.deepcopy(p);bad['listing']['request'].update(change)
            with self.assertRaises(PlanError):site_plan({'sites':{SITES[2]:bad}},SITES[2])
        c,_=load(SITES[0]);p=raw_plan(SITES[0]);p['adapter']['followup_args']=['followup']
        with self.assertRaises(ContractError):Adapter(SITES[0],p['adapter'],None,crawler=c)

    def test_all_fields_match_actual_registered_crawl(self):
        import contextlib,io
        for s,fallback in [(s,False) for s in SITES]+[(SITES[1],True)]:
            with self.subTest(site=s,fallback=fallback):
                c,_=load(s);f=fixture(s);t=Transport(s,fallback=fallback);saved=[]
                def native_get(url,params=None,as_json=True):
                    response=t.request('GET',build_url(url,params or {}))
                    return response.json() if as_json else response.text()
                def native_curl(url,**kw):
                    if url.endswith('/browse') and kw.get('method')!='POST':return '<html></html>'
                    method=kw.get('method','HEAD' if kw.get('head') else 'GET')
                    response=t.request(method,url,body=kw.get('data').encode() if kw.get('data') is not None else None)
                    if method=='HEAD':return '\r\n'.join(f'{k}: {v}' for k,v in response.headers.items())
                    return response.text()
                c._get=native_get;c._curl=native_curl;c.delivery_cursor={};c.request_delay=0;c._delay=0;c.detail_delay=0;c._last_save_created=True
                c._advance_cursor=lambda *a,**k:None;c._mark_exhausted=lambda:None
                c._save_paper=lambda p:saved.append(copy.deepcopy(p))
                with patch('time.sleep',lambda *a:None),contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(2,c.crawl(limit=2))
                actual,_=self.pipeline(s,fallback=fallback)
                self.assertEqual([clean_paper(p) for p in saved],actual)
                self.assertEqual([paper_identity(p) for p in saved],[paper_identity(p) for p in actual])


class HttpExtensions(NoNetwork):
    def fetcher(self,handler,**kwargs):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        class Wire:
            def __init__(self):self.calls=[]
            def request(self,method,url,*,headers=None,body=None):
                self.calls.append((method,url,headers,body));return handler(method,url,headers,body)
        wire=Wire();f=Fetcher('test',wire,Path(tmp.name),interval=0,retries=1,**kwargs)
        return f,wire

    def test_post_and_head_get_robots_first_and_count_budget(self):
        f,t=self.fetcher(lambda m,u,h,b:Response(404 if u.endswith('robots.txt') else 200,{},b'',u),max_requests=3)
        f.request('POST','https://www.etera.ee/api/browse',json_body={'start':0})
        f.request('HEAD','https://www.etera.ee/file.pdf')
        self.assertEqual(['GET','POST','HEAD'],[c[0] for c in t.calls]);self.assertEqual(3,f.requests_made)
        self.assertEqual('application/json',t.calls[1][2]['Content-Type']);self.assertEqual({'start':0},json.loads(t.calls[1][3]))
        with self.assertRaises(BudgetExhausted):f.get('https://www.etera.ee/next')

    def test_robots_disallow_post_and_head_never_sends_either(self):
        for method in ['POST','HEAD']:
            f,t=self.fetcher(lambda m,u,h,b:Response(200,{},'User-agent: *\nDisallow: /',u))
            with self.assertRaises(RobotsDisallowed):f.request(method,'https://www.etera.ee/api/browse',**({'json_body':{}} if method=='POST' else {}))
            self.assertEqual(1,len(t.calls));self.assertEqual('GET',t.calls[0][0])

    def test_private_post_and_head_are_blocked_before_transport(self):
        for method in ['POST','HEAD']:
            f,t=self.fetcher(lambda *args:Response(200,{},''))
            with self.assertRaises(PrivateDestination):f.request(method,'http://127.0.0.1/api/browse',**({'json_body':{}} if method=='POST' else {}))
            self.assertEqual([],t.calls)

    def test_post_redirect_never_replays_body_or_changes_method(self):
        for status in [301,302,303,307,308]:
            f,t=self.fetcher(lambda m,u,h,b:Response(404 if u.endswith('/robots.txt') else status,{'Location':'https://elsewhere.test/write'},'',u))
            with self.assertRaises(PermanentFailure) as ctx:f.request('POST','https://www.etera.ee/api/browse',json_body={'start':0})
            self.assertEqual('post_redirect_requires_review',ctx.exception.reason);self.assertEqual(['GET','POST'],[c[0] for c in t.calls])

    def test_head_redirect_to_private_or_login_keeps_policy(self):
        for location,exc in [('http://127.0.0.1/private',PrivateDestination),('https://www.etera.ee/login',AccessBlocked)]:
            f,t=self.fetcher(lambda m,u,h,b:Response(404 if u.endswith('/robots.txt') else 302,{'Location':location},'',u))
            with self.assertRaises(exc):f.request('HEAD','https://www.etera.ee/file.pdf')
            self.assertEqual(2,len(t.calls))

    def test_refusal_and_429_do_not_retry_or_fallback(self):
        for status,exc in [(401,AccessBlocked),(403,AccessBlocked),(429,RateLimited)]:
            f,t=self.fetcher(lambda m,u,h,b:Response(404 if u.endswith('/robots.txt') else status,{},'',u),rate_limit_stop=1)
            with self.assertRaises(exc):f.request('POST','https://www.etera.ee/api/browse',json_body={})
            self.assertEqual(2,len(t.calls))

    def test_body_limit_nan_and_unsupported_method_fail_before_network(self):
        for method,body in [('PUT',None),('POST',{'x':float('nan')}),('POST',{'x':'x'*65537}),('GET',{})]:
            f,t=self.fetcher(lambda *args:Response(200))
            with self.assertRaises(PolicyRefusal):f.request(method,'https://www.etera.ee/api/browse',json_body=body)
            self.assertEqual([],t.calls)

    def test_production_transport_head_never_reads_body_or_rejects_large_file_length(self):
        class Reply:
            status_code=200;headers={'Content-Length':'999999999'};url='https://x.test/file.pdf'
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def iter_content(self,*a):raise AssertionError('HEAD attempted body read')
        t=object.__new__(RequestsTransport);t.max_bytes=1;t.timeout=(1,1)
        t._requests=SimpleNamespace(RequestException=RuntimeError)
        t.session=SimpleNamespace(request=lambda *a,**kw:Reply())
        self.assertEqual(b'',t.request('HEAD',Reply.url).body)

    def test_real_gate_keeps_post_head_get_15s_after_each_response(self):
        now=[1000.0];starts=[];ends=[]
        def handler(method,url,headers,body):
            starts.append(now[0]);now[0]+=0.25;ends.append(now[0]);return Response(404 if url.endswith('robots.txt') else 200,{},'',url)
        f,t=self.fetcher(handler);f.interval=1.5;f.clock=lambda:now[0]
        def sleep(seconds):now[0]+=seconds
        with patch('delivery.health.request_gate.time.time',lambda:now[0]),patch('delivery.health.request_gate.time.sleep',sleep):
            f.request('POST','https://www.etera.ee/api/browse',json_body={'start':0})
            f.request('HEAD','https://www.etera.ee/file.pdf')
            f.get('https://www.etera.ee/meta')
        self.assertEqual(4,len(starts))
        self.assertTrue(all(starts[i]-ends[i-1]>=1.5 for i in range(1,len(starts))))

    def test_post_endpoint_whitelist_is_not_a_write_api(self):
        f,t=self.fetcher(lambda *a:Response(200))
        with self.assertRaises(PolicyRefusal):f.request('POST','https://www.etera.ee/api/delete',json_body={})
        self.assertEqual([],t.calls)
        p=raw_plan(SITES[2]);p['listing']['url']='https://write.test/api/browse'
        with self.assertRaises(PlanError):site_plan({'sites':{SITES[2]:p}},SITES[2])

    def test_head_redirect_remains_head_and_rechecks_destination_robots(self):
        def handler(m,u,h,b):
            if u.endswith('/robots.txt'):return Response(404,{},'',u)
            if u=='https://a.test/f':return Response(302,{'Location':'https://b.test/f'},'',u)
            return Response(200,{},'',u)
        f,t=self.fetcher(handler);f.request('HEAD','https://a.test/f')
        self.assertEqual(['GET','HEAD','GET','HEAD'],[c[0] for c in t.calls])

    def test_production_post_forwards_only_bounded_serialized_body(self):
        captured=[]
        class Reply:
            status_code=200;headers={};url='https://www.etera.ee/api/browse'
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def iter_content(self,*a):return iter([b'{}'])
        t=object.__new__(RequestsTransport);t.max_bytes=100;t.timeout=(1,1);t._requests=SimpleNamespace(RequestException=RuntimeError)
        def request(*a,**kw):captured.append((a,kw));return Reply()
        t.session=SimpleNamespace(request=request)
        self.assertEqual(b'{}',t.request('POST',Reply.url,body=b'{"start":0}').body)
        self.assertEqual(b'{"start":0}',captured[0][1]['data']);self.assertFalse(captured[0][1]['allow_redirects'])


class PrecisionAndContracts(NoNetwork):
    def test_partial_date_storage_and_raw_original_masks(self):
        from crawler.document_dates import normalize_date
        self.assertEqual({'value':None,'raw':'2025-02','status':'partial','precision':'month'},normalize_date('2025-02'))
        r,docs,_=run(SITES[2]);self.assertTrue(all(d['published_date'] is None for d in docs))
        c,_=load(SITES[0]);self.assertIsNone(c._survey_month_date('2025年13月'))

    def test_legacy_fingerprint_does_not_change_without_opt_in(self):
        from crawler.enumeration.plan import SitePlan
        raw={'strategy':'sitemap','sitemap':{'sitemap_url':'https://x.test/sitemap.xml'}}
        keys=['strategy','listing','segments','sitemap','last_page','oldest_sort','overall_total_check']
        expected=hashlib.sha256(json.dumps({k:raw.get(k) for k in keys},sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:16]
        self.assertEqual(expected,SitePlan('legacy',raw).fingerprint)

    def test_followup_schema_refuses_post_headers_and_multiple_requests(self):
        for bad in [{'method':'POST','url':'https://www.etera.ee/api/browse'},
                    {'method':'HEAD','url':'https://x.test/f','headers':{'Cookie':'x'}},
                    [{'method':'GET','url':'https://x.test/a'},{'method':'GET','url':'https://x.test/b'}]]:
            s=SITES[0];c,_=load(s);p=raw_plan(s);a=Adapter(s,p['adapter'],None,crawler=c);f=fixture(s)
            rows=ListingParser(p['listing']['parser'],adapter=a).parse(Response(200,{'Content-Type':'application/json'},json.dumps(f['listing']),build_url(p['listing']['url'],p['listing']['params']))).items
            c._enumeration_followup=lambda *a:bad;t=Transport(s)
            with tempfile.TemporaryDirectory() as root:
                fetcher=Fetcher(s,t,Path(root),interval=0,retries=1)
                with self.assertRaises(ContractError):a.materialize(rows[0],fetcher)
            self.assertFalse(any(x[0]=='HEAD' for x in t.calls))

    def test_followup_signature_and_kwarg_without_method_are_rejected(self):
        p=raw_plan(SITES[0]);c,_=load(SITES[0]);p['adapter']['followup_args']=['item']
        with self.assertRaises(ContractError):Adapter(SITES[0],p['adapter'],None,crawler=c)
        p=raw_plan(SITES[2]);p['adapter']['paper_args']=['item'];p['adapter']['paper_kwargs']={'meta':'followup'}
        with self.assertRaises(ContractError):Adapter(SITES[2],p['adapter'],None,crawler=load(SITES[2])[0])

    def test_raw_source_date_survives_original_item_fallback_and_detail_normalization(self):
        s=SITES[2];c,_=load(s);f=fixture(s);row=c._enumeration_items(f['listing'])[0];meta=f['details'][row['url']];meta.pop('cf22')
        with guarded():p=c._enumeration_paper(row,meta)
        self.assertEqual('20250200000000',json.loads(p['metadata'])['source_date_to_raw']);self.assertEqual('2025-02',p['published_date'])
        s=SITES[1];c,_=load(s);f=fixture(s);row=c._enumeration_items(f['listing'])[0];detail=f['details'][row['url']]['result'];detail['date']='14.06.2025'
        with guarded():p=c._enumeration_paper(row,detail,None)
        self.assertEqual('2025-06-14',p['published_date']);self.assertEqual('14.06.2025',json.loads(p['metadata'])['source_published_date_raw']);self.assertEqual('13.06.2025',json.loads(p['metadata'])['source_listed_date_raw'])

    def test_api_error_with_empty_member_is_not_exhaustion(self):
        for s in SITES:
            c,_=load(s);data=fixture(s)['empty'];data['error']='denied'
            with self.subTest(s=s),self.assertRaises(PermanentFailure):c._enumeration_items(data,*([1] if s==SITES[0] else []))

    def test_unstable_offset_ties_are_detected_by_unique_total(self):
        s=SITES[2]
        class TieTransport(Transport):
            def request(self,method,url,**kw):
                if method=='POST':
                    offset=json.loads(kw['body'])['start']
                    if offset==25:
                        self.calls.append((method,url,kw.get('headers'),kw['body']))
                        return Response(200,{'Content-Type':'application/json'},json.dumps(self.f['listing']),url)
                return super().request(method,url,**kw)
        t=TieTransport(s,total=50);template=copy.deepcopy(t.f['listing']['results']['docs'][0]);meta=copy.deepcopy(next(iter(t.f['details'].values())))
        records=[];details={}
        for number in range(1,26):
            record=copy.deepcopy(template);record.update(obj_id=str(number),path=f'/zoom/{number}/view',date_created='2025-01-01')
            records.append(record);details[f'https://www.etera.ee/api/item/{number}/meta']=copy.deepcopy(meta)
        t.f['listing']['results']={'count':50,'docs':records};t.f['empty']['results']['count']=50;t.f['details']=details
        result,docs,t=run(s,t);self.assertEqual('complete_with_mismatch',result['status'],result);self.assertEqual(25,len(docs))

    def test_source_loop_and_metadata_changes_are_limited_to_raw_dates(self):
        manifest=json.loads((ROOT/'tests/fixtures/large-sites-batch2-invariants.json').read_text())
        for site,expected in manifest.items():
            source=(ROOT/'crawler/sites/custom'/(site+'.py')).read_text();tree=ast.parse(source)
            cls=next(n for n in tree.body if isinstance(n,ast.ClassDef));cls.body=[n for n in cls.body if not isinstance(n,ast.FunctionDef) or not n.name.startswith('_enumeration_')]
            class Restore(ast.NodeTransformer):
                def visit_FunctionDef(self,node):
                    if site==SITES[0] and node.name=='_survey_month_date':return ast.parse(expected['original_month_method']).body[0]
                    return self.generic_visit(node)
                def visit_Dict(self,node):
                    node=self.generic_visit(node);pairs=[(k,v) for k,v in zip(node.keys,node.values) if not isinstance(k,ast.Constant) or k.value not in {'source_published_date_raw','source_listed_date_raw','source_date_to_raw','date_raw'}]
                    node.keys=[k for k,v in pairs];node.values=[v for k,v in pairs];return node
            tree=Restore().visit(tree)
            # Normalize version-specific empty type_params; nonempty parameters
            # are a real source change and fail. Optional Constant.kind=None is
            # also normalized in canonical(), for Python 3.14/3.12 compatibility.
            for node in ast.walk(tree):
                if hasattr(node, 'type_params'):
                    self.assertEqual([], node.type_params)
                    node._fields=tuple(k for k in node._fields if k != 'type_params')
            def canonical(node):
                if isinstance(node,ast.AST):
                    return [type(node).__name__,[(k,canonical(v)) for k,v in ast.iter_fields(node) if k != 'type_params' and not (k == 'kind' and v is None)]]
                if isinstance(node,list):return [canonical(v) for v in node]
                if isinstance(node,bytes):return {'bytes_hex':node.hex()}
                return node
            actual=hashlib.sha256(json.dumps(canonical(tree),ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
            self.assertEqual(expected['canonical_ast_sha256'],actual,site)

    def test_ots_explicit_other_emitter_is_not_registered_scope(self):
        c,_=load(SITES[1]);f=fixture(SITES[1]);f['listing']['result'][0]['emittentId']='55'
        with self.assertRaises(PermanentFailure):c._enumeration_items(f['listing'])
        d=next(iter(f['details'].values()));d['result']['emittentId']='55'
        with self.assertRaises(PermanentFailure):c._enumeration_detail(Response(200,{'Content-Type':'application/json'},json.dumps(d)),{})
