import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from crawler.sites import CRAWLERS
from delivery.incremental_status import eligibility
from tests.test_incremental_accc import DENIAL

SITE='app-mps-gov-cn-gdnps'
BODY='Controlled substantive body for API detail request measurement and original list identity preservation. '*2


class MPSTests(unittest.TestCase):
    def setUp(self):
        self.c=CRAWLERS[SITE](None,delay=0);self.addCleanup(self.c._session.close);self.g=self.c.crawl.__globals__
        self.enterContext(contextlib.redirect_stdout(io.StringIO()));self.enterContext(patch('time.sleep'))

    def test_partial_curl_is_rejected(self):
        with patch.object(self.g['subprocess'],'run',return_value=SimpleNamespace(returncode=28,stdout='partial',stderr='')) as get:
            self.assertIsNone(self.g['_curl_fetch']('https://example.test','fixture'));self.assertIn('-fsk',get.call_args.args[0])

    def test_list_fetch_failure_is_failed(self):
        with patch.dict(self.g,{'_curl_fetch':lambda *a:None}):self.assertEqual(self.c.crawl(),0)
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed')

    def test_wrong_json_shape_not_empty_source(self):
        for raw in ('{}','[]','{"resultMap":null}','{"resultMap":[null]}'):
            with patch.dict(self.g,{'_curl_fetch':lambda *a:raw}),self.assertRaises(self.g['SourceFailure']) as err:
                self.g['_fetch_with_retry']('https://example.test','fixture')
            self.assertEqual(err.exception.reason,'invalid_response')

    def test_challenge_is_explicit(self):
        with patch.dict(self.g,{'_curl_fetch':lambda *a:DENIAL}):self.assertEqual(self.c.crawl(),0)
        self.assertEqual(self.c.delivery_source_failure,'authentication_failed')

    def test_detail_wrong_ID_is_rejected(self):
        with patch.dict(self.g,{'_fetch_with_retry':lambda *a:{'resultMap':[{'id':'999'}]}}):
            self.assertIsNone(self.c._fetch_detail('123'))
        self.assertEqual(self.c.delivery_source_failure,'invalid_response')

    def test_multiple_details_are_not_arbitrarily_selected(self):
        with patch.dict(self.g,{'_fetch_with_retry':lambda *a:{'resultMap':[{'id':'123'},{'id':'456'}]}}):
            self.assertIsNone(self.c._fetch_detail('123'))
        self.assertEqual(self.c.delivery_source_failure,'invalid_response')

    def test_matching_detail_is_preserved(self):
        with patch.dict(self.g,{'_fetch_with_retry':lambda *a:{'resultMap':[{'id':123,'title':'Fixture'}]}}):
            self.assertEqual(self.c._fetch_detail('123'),{'id':123,'title':'Fixture'})
        self.assertIsNone(self.c.delivery_source_failure)

    def test_nested_detail_failure_does_not_issue_nine_requests(self):
        calls=[]
        def fetch(url,*args):
            calls.append(url)
            return 'cb({"resultMap":[{"id":"123","title":"Fixture"}]})' if url==self.c._list_url(1) else None
        with patch.dict(self.g,{'_curl_fetch':fetch}):self.assertEqual(self.c.crawl(),0)
        self.assertEqual(len(calls),4);self.assertEqual(self.c.delivery_source_failure,'fetch_failed')

    def test_hold_keeps_baseline_incomplete(self):
        r=eligibility(SITE,type(self.c));self.assertEqual(r['classification'],'full_rescan_required')
        self.assertIn('native_unfiltered_scope_equivalence',r['reason']);self.assertFalse(r['baseline_complete'])


if __name__=='__main__':unittest.main()
