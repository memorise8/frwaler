"""Fail-closed contracts, exact bindings and source-total reconciliation, offline."""
import copy
import json
import unittest
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.contracts import ContractError
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from crawler.enumeration.urls import build_url
from tests.collector_split_helpers import load, run_split, split_fixture, split_plan


class ContractEdges(unittest.TestCase):
    def setUp(self):
        self.attempts=[]
        def denied(*args,**kw):
            self.attempts.append('attempted I/O')
            raise AssertionError('offline contract attempted I/O')
        for name in ['socket.getaddrinfo','socket.socket.connect','socket.socket.connect_ex','subprocess.Popen._execute_child']:
            p=patch(name,denied);p.start();self.addCleanup(p.stop)
        self.addCleanup(lambda:self.assertEqual([],self.attempts))

    def parse(self, site, body, url=None):
        raw=split_plan(site);p=site_plan({'version':1,'sites':{site:raw}},site,registered=[site]);crawler,_=load(site)
        adapter=Adapter(site,p.section('adapter'),None,crawler=crawler)
        return ListingParser(p.listing['parser'],adapter=adapter).parse(Response(
            200,{'Content-Type':split_fixture(site)['content_type']},json.dumps(body) if not isinstance(body,str) else body,
            url or build_url(p.listing['url'],p.listing.get('params'))))

    def test_wp_other_400_cannot_finish(self):
        for body in ['<html>Bad request</html>',json.dumps({'code':'rest_invalid_param','data':{'status':400}})]:
            with self.subTest(body=body):
                r,_=run_split('kit-nl-institute',end=lambda url:Response(400,{'Content-Type':'application/json'},body,url),total=2)
                self.assertEqual(('incomplete','listing_failed:unverified_error_status_400'),(r['status'],r['stop_reason']))

    def test_wp_reported_total_mismatch_is_preserved(self):
        r,docs=run_split('kit-nl-institute',total=3)
        self.assertEqual(('complete_with_mismatch','reported_total_mismatch'),(r['status'],r['stop_reason']))
        self.assertEqual(2,len(docs))

    def test_edi_reported_total_mismatch_is_preserved(self):
        s='edi-admin-ch-de';f=split_fixture(s);data=json.loads(f['listing_body']);data['pageResults']=3
        f['listing_body']=json.dumps(data)
        r,docs=run_split(s,fixture=f,end=lambda url:Response(200,{'Content-Type':'application/json'},f['empty_body'],url))
        self.assertEqual(('complete_with_mismatch','reported_total_mismatch'),(r['status'],r['stop_reason']))
        self.assertEqual(2,len(docs))

    def test_wp_identifier_uncertainty_rejects_listing(self):
        s='kit-nl-institute';records=json.loads(split_fixture(s)['listing_body'])
        for value in [None,'101',True,0]:
            with self.subTest(value=value):
                bad=copy.deepcopy(records);bad[0]['id']=value
                with self.assertRaises(PermanentFailure):self.parse(s,bad)

    def test_edi_identifier_or_shape_uncertainty_rejects_listing(self):
        s='edi-admin-ch-de';data=json.loads(split_fixture(s)['listing_body'])
        for key,value in [('id',None),('langGroupId',''),('content',[])]:
            with self.subTest(key=key):
                bad=copy.deepcopy(data);bad['items'][0][key]=value
                with self.assertRaises(PermanentFailure):self.parse(s,bad)

    def test_nesc_error_or_inconsistent_envelope_rejects(self):
        s='nesc-ie-publications';d=json.loads(split_fixture(s)['listing_body'])
        for bad in [dict(success=False,data=d['data']),dict(success=True,data={'pages':1,'html':'<main/>'}),
                    dict(success=True,data={'pages':0,'html':d['data']['html']}),dict(success=True,data={'pages':True,'html':''})]:
            with self.subTest(bad=bad):
                with self.assertRaises(PermanentFailure):self.parse(s,bad)

    def test_query_bindings_refuse_missing_noninteger_or_duplicate_page(self):
        for s in ['education-ni-gov-uk-news','culture-gouv-fr-espace-documentation']:
            raw=split_plan(s);f=split_fixture(s)
            self.assertFalse(raw['last_page']['check_unparameterized_first_page'])
            for suffix in ['', '?page=bad','?page=-1','?page=1&page=2']:
                with self.subTest(site=s,suffix=suffix):
                    with self.assertRaises(ContractError):self.parse(s,f['listing_body'],raw['listing']['url']+suffix)

    def test_page_binding_retains_source_metadata_after_first_page(self):
        c,_=load('culture-gouv-fr-espace-documentation');f=split_fixture(c.site_id)
        with guarded(): rows=c._enumeration_items(f['listing_body'],3)
        self.assertTrue(all(r['list_page_url']==c._build_list_url(3) for r in rows))
        c,_=load('education-ni-gov-uk-news');f=split_fixture(c.site_id)
        with guarded(): rows=c._enumeration_items(f['listing_body'],3)
        self.assertEqual([3,3],[r['page_num'] for r in rows])

    def test_offset_and_repeated_filters_match_native_query(self):
        from urllib.parse import parse_qs,urlsplit
        for s,size in [('mimit-gov-it-it',10),('olympias-lib-uoi-gr-jspui',20),('helios-eie-gr-helios',100),('regulation-govt-nz-about-us',12),('edi-admin-ch-de',20)]:
            with self.subTest(site=s):
                p=split_plan(s);self.assertEqual(('offset',size),(p['listing']['page_mode'],p['listing']['page_size']))
        p=split_plan('edi-admin-ch-de');q=parse_qs(urlsplit(build_url(p['listing']['url'],p['listing']['params'])).query)
        self.assertEqual(['CONTENT_HUB','ONSB'],q['newsKinds']);self.assertEqual(['3'],q['publisherIDs'])
        p=split_plan('kit-nl-institute');self.assertEqual(34,p['listing']['params']['kit_publication_type'])
        self.assertEqual([{'status':400,'json_code':'rest_post_invalid_page_number'}],p['listing']['end_statuses'])

    def test_no_made_up_numeric_id_when_source_uses_slug_or_handle(self):
        s='mimit-gov-it-it';c,_=load(s);f=split_fixture(s);url,body=next(iter(f['details'].items()))
        import re
        body=re.sub(r'<input[^>]*>','',body)
        with guarded(): d=c._parse_detail_page(body,url);p=c._enumeration_paper({'url':url},d)
        self.assertEqual('offline-1',p['external_id']);self.assertEqual('offline-1',p['post_number'])

    def test_paper_threshold_checks_the_actual_source_abstract(self):
        for s,limit in [('finance-ni-gov-uk-publications',100),('mimit-gov-it-it',100),('economy-ni-gov-uk-news',50)]:
            c,_=load(s)
            item={'url':'https://example.org/report','title':'Title','listed_date':'2025-01-01','category':''}
            if s=='finance-ni-gov-uk-publications': detail={'abstract':'x'*(limit-1)}
            elif s=='mimit-gov-it-it':detail={'title':'Title','abstract':'x'*(limit-1),'slug':'report'}
            else:detail=dict(title='Title',abstract='x'*(limit-1),published_date='',node_id='1',slug='report',pdf_url=None,pdf_filename=None)
            with guarded(): self.assertIsNone(c._enumeration_paper(item,detail))

    def test_main_with_maintenance_text_is_not_an_empty_page(self):
        for s in ['finance-ni-gov-uk-publications','mimit-gov-it-it','economy-ni-gov-uk-news',
                  'education-ni-gov-uk-news','culture-gouv-fr-espace-documentation','olympias-lib-uoi-gr-jspui',
                  'helios-eie-gr-helios','regulation-govt-nz-about-us','publichealthscotland-scot-publications']:
            with self.subTest(site=s):
                r,docs=run_split(s,listing='<html><main>Site under maintenance</main></html>')
                self.assertEqual('incomplete',r['status'],r)
                self.assertTrue(r['stop_reason'].startswith('listing_failed:listing_parse_error:'),r)
                self.assertEqual([],docs)
