"""Opt-in contracts: synthetic input only, no network or database connections."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, paper_identity
from crawler.enumeration.contracts import ContractError, validate_contract
from crawler.enumeration.guard import AdapterUngatedNetwork
from crawler.enumeration.http import Response, PermanentFailure
from crawler.enumeration.parsing import Item, ListingParser
from crawler.enumeration.plan import site_plan, PlanError


class Crawler:
    site_id = 'contract-test'

    def listing(self, html):
        return [{'url': 'https://example.test/item', 'external_id': 'native', 'published_date': '2026-10-12'}]

    def detail(self, html, item):
        return {'external_id': item['external_id'], 'url': item['url'], 'abstract': html}

    def paper(self, item, detail):
        return {**item, **detail, 'title': 'Original title'}


class ContractTests(unittest.TestCase):
    def cfg(self, **kw):
        return dict(type='crawler', contract_version=1, list_method='listing', detail_method='detail', **kw)

    def adapter(self, cfg=None, crawler=None):
        return Adapter('contract-test', cfg or self.cfg(), None, crawler=crawler or Crawler())

    def response(self, body='<main></main>', **kw):
        return Response(200, {}, body, kw.get('url', 'https://example.test/list?page=2'))

    def item(self, **kw):
        row = dict(url='https://example.test/item', external_id='native', **kw)
        return Item('native', row['url'], payload=row)

    def parser(self, a):
        return ListingParser({'type':'adapter', 'listing_marker':'main'}, adapter=a)

    def test_dict_rows_preserve_fields(self):
        a = self.adapter(); rows = a.parse_list(self.response())
        self.assertEqual(rows, Crawler().listing(''))
        self.assertEqual(self.parser(a).parse(self.response()).items[0].payload, rows[0])

    def test_tuple_envelope_explicit(self):
        c = Crawler(); c.listing = lambda html: (Crawler().listing(html), True)
        a = self.adapter(self.cfg(list_result=[0]), c)
        self.assertEqual(len(a.parse_list(self.response())), 1)
        with self.assertRaisesRegex(ContractError, 'list_result_not_list'):
            self.adapter(crawler=c).parse_list(self.response())

    def test_tuple_rows(self):
        c=Crawler(); c.listing=lambda html:[('id','/item',None)]
        a=self.adapter(self.cfg(list_row={'type':'tuple','fields':['external_id','url','published_date']}),c)
        self.assertEqual(a.parse_list(self.response()),[{'external_id':'id','url':'/item','published_date':None}])

    def test_url_rows_no_synthetic_id(self):
        c=Crawler();c.listing=lambda html:['/item']
        a=self.adapter(self.cfg(list_row={'type':'url'}),c)
        self.assertEqual(a.parse_list(self.response()),[{'url':'/item'}])
        self.assertNotIn('external_id',self.parser(a).parse(self.response()).items[0].payload)

    def test_json_rows_empty_and_missing_path_differ(self):
        c=Crawler();c.paper=lambda item:dict(item)
        a=self.adapter({'type':'crawler','contract_version':1,'list_source':'json','list_result':['results'], 'paper_method':'paper'},c)
        self.assertTrue(self.parser(a).parse(self.response('{"results":[]}')).extra['explicit_empty'])
        with self.assertRaisesRegex(ContractError,'missing_path'):
            self.parser(a).parse(self.response('{}'))

    def test_nested_row_path(self):
        c=Crawler();c.listing=lambda html:[{'entry':{'url':'/item','external_id':'a'}}]
        self.assertEqual(self.adapter(self.cfg(row_path=['entry']),c).parse_list(self.response())[0]['external_id'],'a')

    def test_unknown_row_and_unserializable_fail(self):
        for value in [None,{},[None],[1],[('/item',)], [{'url':'/item','x':object()}],[{'url':'/item','x':float('nan')}]]:
            with self.subTest(value=value):
                c=Crawler();c.listing=lambda html,value=value:value
                with self.assertRaises(ContractError):self.adapter(crawler=c).parse_list(self.response())

    def test_tuple_width_and_url_types_fail(self):
        for schema,row in [({'type':'tuple','fields':['url']},('url','extra')),({'type':'tuple','fields':['url']},['url']),({'type':'url'},12),({'type':'url'},'')]:
            c=Crawler();c.listing=lambda html:[row]
            with self.subTest(schema=schema,row=row),self.assertRaises(ContractError):
                self.adapter(self.cfg(list_row=schema),c).parse_list(self.response())

    def test_missing_listing_identity_refused(self):
        for row in [{},{'title':'not an id'},{'url':{'bad':'url'}},{'external_id':True},{'external_id':['bad']}]:
            c=Crawler();c.listing=lambda html:[row]
            with self.subTest(row=row),self.assertRaises(ContractError):self.parser(self.adapter(crawler=c)).parse(self.response())

    def test_query_item_detail_and_kwargs(self):
        c=Crawler();c.listing=lambda *,number:[{'url':'/item','external_id':str(number)}]
        c.detail=lambda html,*,ident:{'url':'https://example.test/item','external_id':ident,'abstract':html}
        c.paper=lambda item,*,parsed:{**item,**parsed,'title':'native'}
        cfg=self.cfg(list_args=[],list_kwargs={'number':{'source':'query','path':['page'],'type':'int'}},detail_args=['html'],detail_kwargs={'ident':{'source':'item','path':['external_id']}},paper_method='paper',paper_args=['item'],paper_kwargs={'parsed':'detail'})
        a=self.adapter(cfg,c);row=a.parse_list(self.response())[0]
        self.assertEqual(row['external_id'],'2')
        self.assertEqual(a.materialize(self.item(),SimpleNamespace(get=lambda url:self.response('body')))['title'],'native')

    def test_missing_field_never_defaulted(self):
        a=self.adapter(self.cfg(detail_args=['html',{'source':'item','path':['missing']}]))
        with self.assertRaisesRegex(ContractError,'missing_path'):a.materialize(self.item(),SimpleNamespace(get=lambda url:self.response()))

    def test_bad_query_refused(self):
        c=Crawler();c.listing=lambda number:[]
        a=self.adapter(self.cfg(list_args=[{'source':'query','path':['page'],'type':'int'}]),c)
        for suffix in ['', '?page=x', '?page=1&page=2']:
            with self.subTest(suffix=suffix),self.assertRaises(ContractError):a.parse_list(self.response(url='https://example.test/list'+suffix))

    def test_signature_validation_precedes_fetch(self):
        with self.assertRaisesRegex(ContractError,'method_signature'):self.adapter(self.cfg(detail_args=[]))

    def test_detail_then_registered_paper_identity(self):
        cfg=self.cfg(paper_method='paper',paper_args=['item','detail']);c=Crawler();a=self.adapter(cfg,c);item=self.item()
        paper=a.materialize(item,SimpleNamespace(get=lambda url:self.response('body')))
        expected=c.paper(item.payload,c.detail('body',item.payload))
        self.assertEqual(paper_identity(paper),paper_identity(expected));self.assertEqual(paper['external_id'],'native')

    def test_paper_only_no_fetch(self):
        c=Crawler();c.paper=lambda item:{**item,'title':'native'}
        a=self.adapter({'type':'crawler','contract_version':1,'list_method':'listing','paper_method':'paper'},c)
        self.assertEqual(a.materialize(self.item(),None)['external_id'],'native')

    def test_list_returns_paper_no_fetch(self):
        a=self.adapter({'type':'crawler','contract_version':1,'list_method':'listing','list_returns_paper':True})
        self.assertEqual(a.materialize(self.item(),None)['external_id'],'native')

    def test_missing_identity_or_paper_shape_refused(self):
        for paper in [[], 'url', {'url':'https://example.test/item'}, {'external_id':'id'}, {'external_id':'id','url':'x','site_id':'other'}, {'external_id':['uncertain'],'url':'https://example.test/item'}, {'external_id':True,'url':'https://example.test/item'}]:
            c=Crawler();c.detail=lambda html,item:paper
            with self.subTest(paper=paper),self.assertRaises(ContractError):self.adapter(crawler=c).materialize(self.item(),SimpleNamespace(get=lambda url:self.response()))

    def test_none_detail_does_not_call_paper(self):
        c=Crawler();c.detail=lambda html,item:None;c.paper=lambda item,detail:self.fail('must not call')
        self.assertIsNone(self.adapter(self.cfg(paper_method='paper',paper_args=['item','detail']),c).materialize(self.item(),SimpleNamespace(get=lambda url:self.response())))

    def test_login_still_blocks(self):
        resp=self.response();resp.login_suspect=True
        with self.assertRaises(PermanentFailure):self.adapter().materialize(self.item(),SimpleNamespace(get=lambda url:resp))

    def test_network_and_process_methods_still_guarded(self):
        import socket,subprocess
        def network(html):
            with socket.socket() as sock:sock.connect(('1.1.1.1',80))
        c=Crawler();c.listing=network
        with self.assertRaises(AdapterUngatedNetwork):self.adapter(crawler=c).parse_list(self.response())
        c.listing=lambda html:subprocess.run(['must-not-run'])
        with self.assertRaises(AdapterUngatedNetwork):self.adapter(crawler=c).parse_list(self.response())

    def test_invalid_contracts(self):
        cases=[{'contract_version':True},{'contract_version':2},{'list_result':'0'},{'list_row':{'type':'auto'}},{'list_row':{'type':'tuple','fields':['a','a']}},{'list_args':[{'source':'item','path':['x']}]},{'detail_args':['detail']},{'list_source':'json'},{'detail_args':[{'source':'item','path':['x'],'default':'guess'}]},{'id_regex':'.+'}]
        for update in cases:
            with self.subTest(update=update),self.assertRaises(ContractError):validate_contract({**self.cfg(),**update})
        with self.assertRaises(ContractError):validate_contract({'type':'crawler','list_result':[0]})

    def test_contract_fingerprint_and_sitemap_rules(self):
        p={'strategy':'last_page_backward','listing':{'url':'https://example.test/list','parser':{'type':'adapter'}},'adapter':self.cfg()}
        def sp(p):return site_plan({'sites':{'s':p}},'s',registered=['s'])
        fp=sp(p).fingerprint;p['adapter']['list_result']=[0];self.assertNotEqual(fp,sp(p).fingerprint)
        p.update(strategy='sitemap',sitemap={'sitemap_url':'https://example.test/sitemap.xml'})
        p['adapter']['detail_args']=['html',{'source':'item','path':['url']}]
        with self.assertRaises(PlanError):sp(p)

    def test_deep_copy_preserves_original_payload(self):
        c=Crawler();c.detail=lambda html,item:(item.update(external_id='changed') or {'external_id':'native','url':'https://example.test/item'})
        item=self.item();self.adapter(crawler=c).materialize(item,SimpleNamespace(get=lambda url:self.response()))
        self.assertEqual(item.payload['external_id'],'native')

    def test_paper_response_without_detail_rejected(self):
        c=Crawler();c.paper=lambda detail: detail
        with self.assertRaisesRegex(ContractError,'paper_response_without_detail'):
            self.adapter({'type':'crawler','contract_version':1,'list_method':'listing','paper_method':'paper','paper_args':['detail']},c)

    def test_paper_invalid_url_or_blank_id_rejected(self):
        for paper in [{'external_id':'id','url':{}},{'external_id':'id','url':{'bad':'url'}},{'external_id':'   ','url':'https://example.test/item'}]:
            c=Crawler();c.detail=lambda html,item:paper
            with self.subTest(paper=paper),self.assertRaises(ContractError):
                self.adapter(crawler=c).materialize(self.item(),SimpleNamespace(get=lambda url:self.response()))

    def test_detail_and_paper_process_tripwire(self):
        import subprocess
        def bad(*args):return subprocess.run(['must-not-run'])
        c=Crawler();c.detail=bad
        with self.assertRaises(AdapterUngatedNetwork):
            self.adapter(crawler=c).materialize(self.item(),SimpleNamespace(get=lambda url:self.response()))
        c=Crawler();c.paper=bad
        with self.assertRaises(AdapterUngatedNetwork):
            self.adapter(self.cfg(paper_method='paper',paper_args=['item','detail']),c).materialize(self.item(),SimpleNamespace(get=lambda url:self.response()))

    def test_url_token_is_item_url_after_redirect(self):
        seen={}
        c=Crawler()
        def detail(html,url,*,final):
            seen.update(url=url,final=final);return {'external_id':'native','url':url,'title':'t'}
        c.detail=detail
        a=self.adapter(self.cfg(detail_args=['html','url'],detail_kwargs={'final':'final_url'}),c)
        paper=a.materialize(self.item(),SimpleNamespace(get=lambda url:self.response(url=url+'?moved=1')))
        self.assertEqual({'url':'https://example.test/item','final':'https://example.test/item?moved=1'},seen)
        self.assertEqual('https://example.test/item',paper_identity(paper)[1])
        c.detail=lambda html,url:{'external_id':'native','url':url,'title':'t'}
        a=self.adapter(self.cfg(detail_args=['html','request_url']),c)
        self.assertEqual('https://example.test/item',a.materialize(self.item(),SimpleNamespace(get=lambda url:self.response(url=url+'-x')))['url'])

    def test_request_url_token_not_in_listing(self):
        with self.assertRaisesRegex(ContractError,r'^adapter_contract:invalid_argument_token:request_url$'):
            validate_contract(self.cfg(list_args=['html','request_url']))

    def test_copies_of_crawler_objects_run_guarded(self):
        from crawler.enumeration import guard
        seen=[]
        class Lazy(dict):
            def __deepcopy__(self,memo):
                seen.append(guard.active());return dict(self)
        c=Crawler()
        c.listing=lambda html:[{'url':'/a','external_id':'1','x':Lazy(k='v')}]
        c.detail=lambda html,item:Lazy(external_id='1',url='https://example.test/a')
        c.paper=lambda item,detail:{**detail,'title':'t'}
        a=self.adapter(self.cfg(paper_method='paper',paper_args=['item','detail']),c)
        a.parse_list(self.response())
        a.materialize(self.item(),SimpleNamespace(get=lambda url:self.response()))
        self.assertEqual([True,True],seen)

    def test_strict_url_field_no_fallback(self):
        c=Crawler();c.listing=lambda html:[{'source_url':'','url':'https://example.test/other','external_id':'a'}]
        p=ListingParser({'type':'adapter','listing_marker':'main','url_field':'source_url'},adapter=self.adapter(crawler=c))
        with self.assertRaisesRegex(ContractError,r'^adapter_contract:listing_url_missing$'):p.parse(self.response())
        # Without a detail request the key alone identifies the row; the other 'url' is never used.
        c.paper=lambda item:dict(item)
        a=self.adapter({'type':'crawler','contract_version':1,'list_method':'listing','paper_method':'paper'},c)
        item=ListingParser({'type':'adapter','listing_marker':'main','url_field':'source_url'},adapter=a).parse(self.response()).items[0]
        self.assertEqual(('a',''),(item.key,item.url))

    def test_detail_requires_listing_url(self):
        c=Crawler();c.listing=lambda html:[{'external_id':'a'}]
        with self.assertRaisesRegex(ContractError,r'^adapter_contract:listing_url_missing$'):
            self.parser(self.adapter(crawler=c)).parse(self.response())

    def test_good_then_bad_row_rejects_whole_page(self):
        c=Crawler();c.listing=lambda html:[{'url':'/good','external_id':'id'},None]
        with self.assertRaises(ContractError):self.parser(self.adapter(crawler=c)).parse(self.response())

    def test_contract_failure_stops_runner_without_save_or_checkpoint(self):
        from crawler.enumeration.adapters import AdapterConfigError
        from crawler.enumeration.runner import SiteRunner
        c=Crawler();c.detail=lambda html,item:[]
        a=self.adapter(crawler=c)
        p=site_plan({'sites':{'contract-test':{'strategy':'last_page_backward','listing':{'url':'https://example.test/list','parser':{'type':'adapter'}},'adapter':self.cfg()}}},'contract-test',registered=['contract-test'])
        def unexpected(*args,**kwargs):self.fail('contract failure must not be saved or swallowed')
        store=SimpleNamespace(item=lambda *args:None,record_item=unexpected)
        a.save=unexpected
        runner=SiteRunner(p,store,SimpleNamespace(get=lambda url:self.response()),a)
        with self.assertRaisesRegex(AdapterConfigError,'adapter_contract:paper_not_dict') as caught:
            runner.process_item(self.item(),'all')
        self.assertEqual(('config_error','adapter_contract:paper_not_dict'),runner._stop_status(caught.exception,{}))
        self.assertEqual(0,runner.stats['failed_permanent'])
