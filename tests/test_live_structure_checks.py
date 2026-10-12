import ast
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from delivery.scripts.live_check_contracts import compile_plan, page_route, at_page, parse_listing, is_empty_boundary, literal_limits
from delivery.scripts.live_check_probes import ProbeSession, probe_target
from delivery.scripts.live_check_transport import LiveTransport, AccessStopped, BudgetReached

URL='https://source.example/list'


def html(number, wrap=False):
    number=0 if wrap else number
    values={0:[9,8,7],1:[6,5,4],2:[3,2,1]}.get(number,[])
    body=''.join(f'<article><a href="/item/{n}">{n}</a><time datetime="2020-01-0{n}"></time></article>' for n in values)
    if not body:body='<div class="view-empty">No results</div>'
    return f'<html><title>List</title><h1>Items</h1><link rel="canonical" href="{URL}"><main>{body}</main></html>'.encode()


class FakeTransport:
    def __init__(self, mapping):self.mapping=mapping;self.calls=[]
    def allowed(self,url,record):return {'status':'test_allow'}
    def hold(self,*args):pass
    def fetch_checked(self,url,purpose,record,error_json=False):
        record.reserve();self.calls.append(url)
        raw=self.mapping(url)
        r={'url':url,'status':200,'sha256':hashlib.sha256(raw).hexdigest(),'content_type':'text/html','purpose':purpose}
        record(r);return r,raw


class LiveCheckTests(unittest.TestCase):
    def plan(self):return dict(live_check='?page=N',selectors={},selector_hints=[],pagination_config={},static_limits=[])

    def session(self,transport,budget=30):
        return ProbeSession(self.plan(),transport,{'requests':[],'request_budget':budget},lambda:None)

    def test_numeric_queue_URLs_share_scope_and_prose_is_preserved(self):
        row=dict(site_id='x',live_check=URL+'?page=N 과 ?page=42',delivery=True,kind='페이지 파라미터')
        original=dict(scopes=[{'url':URL}],profile=dict(list_selectors={},pagination_config={}))
        plan=compile_plan(row,original,{},'MAX_PAGES=200')
        self.assertEqual(len(plan['targets']),1)
        self.assertIn(URL+'?page=42',[x['url'] for x in plan['targets'][0]])
        self.assertEqual(plan['live_check'],row['live_check'])

    def test_query_page_mutation_keeps_filters_and_date_precision(self):
        route=page_route(URL+'?category=press&page=42')
        self.assertEqual(at_page(route,0),URL+'?category=press&page=0')
        parsed=parse_listing(b'<main><article><a href="/item/1">Report 2020</a></article></main>',URL)
        self.assertIsNone(parsed['oldest_observed']['date'])

    def test_boundary_search_observes_zero_first_last_and_adjacent_empty(self):
        from urllib.parse import parse_qs,urlsplit
        def response(url):return html(int(parse_qs(urlsplit(url).query).get('page',['0'])[0]))
        transport=FakeTransport(response);session=self.session(transport)
        scope=probe_target(session,[{'url':URL}])
        self.assertEqual(scope['checks']['first_page_number']['first'],0)
        self.assertEqual(scope['checks']['last_and_after']['last'],2)
        self.assertEqual(scope['checks']['last_and_after']['after_behavior'],'explicit_empty_same_collection')
        self.assertEqual(scope['strategy'],'마지막 페이지부터 거꾸로')
        self.assertEqual(scope['checks']['oldest_item']['item']['date'],'2020-01-01')

    def test_wraparound_is_not_empty_end(self):
        from urllib.parse import parse_qs,urlsplit
        def response(url):
            number=int(parse_qs(urlsplit(url).query).get('page',['0'])[0]);return html(number,wrap=number>2)
        scope=probe_target(self.session(FakeTransport(response)),[{'url':URL}])
        self.assertNotEqual(scope['status'],'confirmed')
        self.assertEqual(scope['checks']['hidden_cap']['status'],'observed_first_page_repeat')

    def test_empty_error_shell_is_not_source_exhaustion(self):
        first={'status':200,'summary':parse_listing(html(0),URL)}
        other={'status':200,'summary':parse_listing(b'<h1>Unavailable</h1><div class="view-empty">No results</div>',URL)}
        self.assertFalse(is_empty_boundary(first,other))

    def test_budget_stops_before_extra_network_attempt(self):
        transport=FakeTransport(lambda url:html(0));session=self.session(transport,1)
        session.read(URL,'first')
        with self.assertRaises(BudgetReached):session.read(URL+'?page=1','second')
        self.assertEqual(len(transport.calls),1)

    def test_detail_and_document_routes_are_not_requested(self):
        transport=FakeTransport(lambda url:html(0));session=self.session(transport)
        for path in ('/report.pdf','/wp-json/wp/v2/posts/123','/2026/10/12/single-article/'):
            with self.assertRaisesRegex(ValueError,'list_only_policy'):session.read('https://source.example'+path,'entry')
        self.assertEqual(transport.calls,[])

    def test_inherited_access_refusal_is_not_retried_for_robots(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);old=p/'old';old.mkdir();new=p/'new';new.mkdir()
            (old/'access-holds.json').write_text(json.dumps({'source.example':{'reason':'access_refused','url':URL}}))
            transport=LiveTransport(new,old)
            with patch.object(transport,'fetch_checked',side_effect=AssertionError('must not fetch')):
                with self.assertRaisesRegex(AccessStopped,'prior_access_hold'):transport.allowed(URL,lambda r:None)

    def test_extra_batch_inherits_primary_refusal_even_without_old_hold_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);primary=p/'live-check-20261012';primary.mkdir()
            (primary/'access-holds.json').write_text(json.dumps({'source.example':{'reason':'access_refused'}}))
            transport=LiveTransport(p/'live-check-extra-20261012',p/'discovery-20261012')
            with patch.object(transport,'fetch_checked',side_effect=AssertionError('must not fetch')):
                with self.assertRaisesRegex(AccessStopped,'prior_access_hold'):transport.allowed(URL,lambda r:None)

    def test_robots_disallow_prevents_target_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);transport=LiveTransport(p/'new',p/'old')
            with patch.object(transport,'fetch_checked',return_value=({'url':'https://source.example/robots.txt','status':200},b'User-agent: *\nDisallow: /list\n')):
                with self.assertRaisesRegex(AccessStopped,'robots_disallow'):transport.allowed(URL,lambda r:None)

    def test_literal_retry_loop_is_not_a_page_cap(self):
        data=literal_limits('MAX_PAGES=200\nfor attempt in range(3): pass\nfor page in range(1, 201): pass\n')
        self.assertEqual(len(data),2);self.assertEqual(data[1]['maximum_number'],200)

    def test_public_search_keys_are_not_credentials(self):
        from delivery.health.scope_inventory import public_url
        self.assertEqual(public_url(URL+'?keys=pdf'),URL+'?keys=pdf')
        self.assertIsNone(public_url(URL+'?api_key=secret'))
        self.assertIsNone(public_url(URL+'?token=secret'))

    def test_instruction_particles_ranges_and_relative_endpoints(self):
        row=dict(site_id='x',live_check=URL+'?page=1과 '+URL+'?page=0..2 및 wp-json/wp/v2/posts 및 sitemap.xml',delivery=True,kind='페이지 파라미터')
        original=dict(scopes=[{'url':URL}],profile=dict(list_selectors={},pagination_config={}))
        plan=compile_plan(row,original,{},'')
        urls={x['url'] for group in plan['targets'] for x in group}
        self.assertTrue({URL+'?page=0',URL+'?page=1',URL+'?page=2','https://source.example/sitemap.xml','https://source.example/wp-json/wp/v2/posts'}<=urls)
        self.assertEqual(plan['unresolved_URLs'],[])

    def test_korean_query_value_is_retained(self):
        from delivery.scripts.live_check_contracts import clean_url
        value=URL+'?org=재정경제부&page=1'
        self.assertEqual(clean_url(value),value)
        self.assertEqual(clean_url(URL+'.xml(또는'),URL+'.xml')

    def test_query_before_explicit_feed_binds_original_list(self):
        row=dict(site_id='x',live_check='?page=1과 ?page=150의 항목 확인. RSS https://source.example/feed?page=0/1/30 확인',delivery=True,kind='페이지 파라미터')
        original=dict(scopes=[{'url':URL}],profile=dict(list_selectors={},pagination_config={}))
        plan=compile_plan(row,original,{},'')
        urls={x['url'] for group in plan['targets'] for x in group}
        self.assertIn(URL+'?page=150',urls)
        self.assertNotIn('https://source.example/feed?page=150',urls)

    def test_query_ranges_and_Korean_placeholder_suffix_are_not_literal_pages(self):
        row=dict(site_id='x',live_check='?page=N을 확인하고 ?page=22~30 비교. ?page=0..N 확인',delivery=True,kind='페이지 파라미터')
        original=dict(scopes=[{'url':URL}],profile=dict(list_selectors={},pagination_config={}))
        plan=compile_plan(row,original,{},'')
        urls={x['url'] for group in plan['targets'] for x in group}
        self.assertIn(URL+'?page=1',urls)
        for n in range(22,31):self.assertIn(URL+'?page='+str(n),urls)
        self.assertFalse(any('..' in u or '~' in u or '%EC' in u for u in urls))
        self.assertTrue(any('0..N' in u['value'] for u in plan['unresolved_URLs']))

    def test_named_native_endpoint_keeps_requested_taxonomy_filter(self):
        row=dict(site_id='x',live_check='institute_pub?kit_publication_type=34&per_page=1&order=asc 총건수',delivery=True,kind='WordPress REST')
        original=dict(scopes=[{'url':URL}],profile=dict(list_selectors={},pagination_config={}))
        endpoint='https://source.example/wp-json/wp/v2/institute_pub'
        plan=compile_plan(row,original,{'endpoints':[{'url':endpoint}]},'')
        urls={x['url'] for group in plan['targets'] for x in group}
        self.assertIn(endpoint+'?kit_publication_type=34&per_page=1&order=asc',urls)
        self.assertNotIn(endpoint,urls)

    def test_public_go_kr_menu_key_is_not_treated_as_secret(self):
        from delivery.health.scope_inventory import public_url
        url='https://www.pps.go.kr/kor/bbs/list.do?key=00664&pageIndex=1'
        self.assertEqual(public_url(url),url)
        self.assertIsNone(public_url(URL+'?key=secret'))

    def test_sitemap_prefix_count_uses_whole_XML_not_ID_sample(self):
        raw=('<urlset>'+''.join(f'<url><loc>https://source.example/publication/{n}</loc></url>' for n in range(25))+'</urlset>').encode()
        result=parse_listing(raw,'https://source.example/sitemap.xml','application/xml',{'live_check':'/publication/ URL 수'})
        self.assertEqual(len(result['items']),16)
        self.assertEqual(result['requested_prefix_counts'][0]['count'],25)
        self.assertEqual(len(result['requested_prefix_counts'][0]['unique_URL_hashes']),25)

    def test_scope_alias_query_does_not_attach_to_previous_collection(self):
        row=dict(site_id='x',live_check=URL+'?page=41 그리고 studies ?page=20',delivery=True,kind='페이지 파라미터')
        original=dict(scopes=[{'url':URL},{'url':'https://source.example/studies'}],profile=dict(list_selectors={},pagination_config={}))
        plan=compile_plan(row,original,{},"SCOPES={'studies':dict(path='/studies',view='.studies')}")
        urls={x['url'] for group in plan['targets'] for x in group}
        self.assertIn('https://source.example/studies?page=20',urls)
        self.assertNotIn(URL+'?page=20',urls)

    def test_query_fragments_preserve_filters_and_expand_page_choices(self):
        row=dict(site_id='x',live_check=URL+'?category=press&page=1과 ?page=2/3 및 ?page=50,200',delivery=True,kind='페이지 파라미터')
        original=dict(scopes=[{'url':URL+'?category=press'}],profile=dict(list_selectors={},pagination_config={}))
        plan=compile_plan(row,original,{},'')
        urls={x['url'] for group in plan['targets'] for x in group}
        self.assertTrue({URL+'?category=press&page='+str(n) for n in (1,2,3,50,200)}<=urls)
        self.assertFalse(any('%2F' in u or '%EA' in u for u in urls))

    def test_repair_reuses_metadata_without_refetch_or_budget_reset(self):
        transport=FakeTransport(lambda url:html(0))
        response=dict(url=URL,status=200,number=1,summary=parse_listing(html(0),URL))
        result={'requests':[response],'request_budget':1}
        session=ProbeSession(self.plan(),transport,result,lambda:None,seed_summaries=True)
        self.assertEqual(session.read(URL,'reuse')[0]['number'],1)
        self.assertEqual(transport.calls,[])
        with self.assertRaises(BudgetReached):session.read(URL,'refresh',repeat=True)
        self.assertEqual(len(result['requests']),1)

    def test_JSON_typed_empty_is_not_an_error_or_unrelated_array(self):
        head={'status':200,'summary':parse_listing(b'{"items":[{"id":1}],"total":1}',URL,'application/json')}
        empty={'status':200,'summary':parse_listing(b'{"items":[],"total":1}',URL,'application/json')}
        error={'status':200,'summary':parse_listing(b'{"items":[],"error":"failure"}',URL,'application/json')}
        other={'status':200,'summary':parse_listing(b'{"results":[],"total":1}',URL,'application/json')}
        self.assertTrue(is_empty_boundary(head,empty))
        self.assertFalse(is_empty_boundary(head,error))
        self.assertFalse(is_empty_boundary(head,other))

    def test_JSON_offset_last_and_adjacent_empty(self):
        from urllib.parse import parse_qs,urlsplit
        def response(url):
            offset=int(parse_qs(urlsplit(url).query).get('offset',['0'])[0])
            records=[dict(id=n,date=f'2020-01-0{n}') for n in range(9,0,-1)]
            return json.dumps(dict(items=records[offset:offset+3],total=9)).encode()
        scope=probe_target(self.session(FakeTransport(response)),[{'url':URL+'?offset=0'}])
        self.assertEqual(scope['status'],'confirmed')
        self.assertEqual(scope['checks']['last_and_after']['last_offset'],6)
        self.assertEqual(scope['checks']['total_arithmetic']['total'],9)
        self.assertEqual(scope['strategy'],'마지막 페이지부터 거꾸로')

    def test_environment_page_cap_is_preserved_as_default_only(self):
        caps=literal_limits('_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))')
        self.assertEqual(caps[0]['value'],200)
        self.assertEqual(caps[0]['kind'],'page_cap_environment_default')
        self.assertFalse(caps[0]['scope_binding_verified'])

    def test_pager_anchors_are_not_records_and_card_dates_are_read(self):
        raw=b'<h1>List</h1><main><a class="card" href="/item/1"><small>Press</small><small>12/10/2020</small></a><nav><a rel="next" href="?page=2">Next</a><a rel="next" href="?page=2">Next</a></nav></main>'
        result=parse_listing(raw,URL,plan={'selector_hints':['a[rel~="next"]','a.card']})
        self.assertEqual(result['item_count'],1)
        self.assertEqual(result['oldest_observed']['date'],'2020-10-12')

    def test_scoped_view_excludes_static_related_items(self):
        raw=b'<main><div class="view-main"><div class="view-content"><div class="views-row"><a href="/item/1">One</a></div></div></div><div class="views-row"><a href="/related">Related</a></div></main>'
        result=parse_listing(raw,URL,plan={'selector_hints':['.view-main','.view-content .views-row']})
        self.assertEqual(result['item_count'],1)
        self.assertEqual(result['items'][0]['id'],'https://source.example/item/1')

    def test_no_429_retry_when_request_budget_exhausted(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);transport=LiveTransport(p/'new',p/'old')
            transport.held['source.example']={'reason':'rate_limited'}
            session=self.session(transport,budget=1)
            def rejected(*args,**kwargs):return {'url':URL,'status':429},None
            with patch('delivery.scripts.live_check_transport.ReconTransport.fetch',side_effect=rejected):
                with self.assertRaises(BudgetReached):transport.fetch_checked(URL,'first',session.record)
            self.assertEqual(transport.held['source.example']['reason'],'rate_limited')
            self.assertEqual(transport.retry_used,set())

    def test_429_gets_only_one_delayed_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            from types import SimpleNamespace
            p=Path(tmp);transport=LiveTransport(p/'new',p/'old');session=self.session(transport)
            clock=[1000.0];calls=[]
            def response(*args,**kwargs):
                calls.append(clock[0]);transport.held['source.example']={'reason':'rate_limited'}
                return {'url':URL,'status':429,'error':'rate_limited'},None
            def sleep(seconds):clock[0]+=seconds
            fake=SimpleNamespace(time=lambda:clock[0],sleep=sleep)
            with patch('delivery.scripts.live_check_transport.time',fake),patch('delivery.scripts.live_check_transport.ReconTransport.fetch',side_effect=response):
                transport.fetch_checked(URL,'first',session.record)
                transport.fetch_checked(URL,'later',session.record)
            self.assertEqual(len(calls),3)
            self.assertGreaterEqual(calls[1]-calls[0],3)
            self.assertEqual(sum('one_rate_limit_retry' in r['purpose'] for r in session.result['requests']),1)
            self.assertEqual(transport.held['source.example']['reason'],'rate_limited')

    def test_offline_audit_preserves_unrequested_URL_gap(self):
        from delivery.scripts.audit_live_structure import audit_site
        plan=dict(site_id='x',delivery=True,kind='페이지 파라미터',live_check='last and next',targets=[[{'url':URL,'provenance':'explicit'}]])
        result=dict(site_id='x',live_check=plan['live_check'],status='unconfirmable_custom',requests=[],scopes=[],request_budget=30,
                    database_connections=0,documents_saved=0,actual_HTTP_responses=0)
        audit=audit_site(plan,result)
        self.assertIn('no_live_or_inherited_evidence',audit['integrity_issues'])
        self.assertFalse(audit['required_URLs'][0]['exact_URL_observed'])

    def test_no_database_or_crawler_execution_in_new_checker(self):
        root=Path(__file__).resolve().parents[1]/'delivery/scripts'
        for filename in ('live_check_contracts.py','live_check_transport.py','live_check_probes.py','live_check_structure.py',
                         'live_count_estimates.py','repair_live_structure.py','audit_live_structure.py','review_live_defects.py','resolve_live_strategies.py'):
            tree=ast.parse((root/filename).read_text())
            called={n.func.attr for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)}
            self.assertFalse(called & {'crawl','_save_paper','open_db','connect','execute','insert_document'},filename)


if __name__=='__main__':unittest.main()
