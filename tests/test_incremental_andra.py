import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crawler.sites import CRAWLERS
from delivery.incremental_status import eligibility

SITE='andra-fr-publications'
CHALLENGE="<html><body><script>window.location.href='/redirect_TEST/fixture';</script><noscript>This website requires JS enabled and cookies</noscript></body></html>"


class AndraTests(unittest.TestCase):
    def setUp(self):
        self.c=CRAWLERS[SITE](None,delay=0);self.addCleanup(self.c._session.close)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()));self.enterContext(patch('time.sleep'))

    def response(self,body,status=200,code=0):
        return SimpleNamespace(returncode=code,stdout=f'HTTP/2 {status}\r\nContent-Type: text/html\r\n\r\n{body}'.encode(),stderr=b'')

    def test_challenge_stops_before_cookie_redirect_or_ajax(self):
        with patch.object(self.c.crawl.__globals__['subprocess'],'run',return_value=self.response(CHALLENGE)) as get:
            self.assertEqual(self.c.crawl(),0);self.assertEqual(get.call_count,1)
        self.assertEqual(self.c.delivery_source_failure,'authentication_failed')

    def test_partial_response_is_not_accepted(self):
        with patch.object(self.c.crawl.__globals__['subprocess'],'run',return_value=self.response('partial',code=28)) as get:
            self.assertIsNone(self.c._curl('https://example.test'));self.assertEqual(get.call_count,3)
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed')

    def test_http_failure_not_empty_success(self):
        with patch.object(self.c.crawl.__globals__['subprocess'],'run',return_value=self.response('Error',status=500)) as get:
            self.assertEqual(self.c.crawl(),0);self.assertEqual(get.call_count,1)
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed')

    def test_proxy_header_and_body_blank_lines_are_preserved(self):
        raw='HTTP/1.1 200 Connection established\r\n\r\nHTTP/2 200\r\nContent-Type: text/html\r\n\r\nFirst\n\nSecond'
        h,b=self.c._split_hb(raw);self.assertTrue(h.startswith('HTTP/2'));self.assertEqual(b,'First\n\nSecond')
        self.assertEqual(self.c._split_hb('First\n\nSecond'),('','First\n\nSecond'))

    def test_ordinary_article_discussing_cookies_is_not_denial(self):
        body='<article><h1>This website requires JS enabled and cookies</h1></article>'
        with patch.object(self.c.crawl.__globals__['subprocess'],'run',return_value=self.response(body)):
            self.assertIn(body,self.c._curl('https://example.test'))
        self.assertIsNone(self.c.delivery_source_failure)

    def test_unknown_verification_redirect_still_not_followed(self):
        with patch.object(self.c,'_curl',return_value="<script>window.location.href='/unknown-verification';</script>") as get:
            self.assertIsNone(self.c._handshake());self.assertEqual(get.call_count,1)
        self.assertEqual(self.c.delivery_source_failure,'authentication_failed')

    def test_ajax_denial_does_not_multiply_retries(self):
        with patch.object(self.c.crawl.__globals__['subprocess'],'run',return_value=self.response(CHALLENGE)) as get:
            self.assertIsNone(self.c._ajax_page(1,'fixture','fixture'));self.assertEqual(get.call_count,1)
        self.assertEqual(self.c.delivery_source_failure,'authentication_failed')

    def test_missing_ajax_insert_is_not_end(self):
        with patch.object(self.c,'_curl',return_value='[]'):
            self.assertIsNone(self.c._ajax_page(1,'fixture','fixture'))
        self.assertEqual(self.c.delivery_source_failure,'invalid_response')

    def test_ordinary_ajax_insert_preserved(self):
        with patch.object(self.c,'_curl',return_value='[{"command":"insert","data":"<article>Fixture</article>"}]'):
            self.assertEqual(self.c._ajax_page(1,'fixture','fixture'),'<article>Fixture</article>')
        self.assertIsNone(self.c.delivery_source_failure)

    def test_reviewed_hold_does_not_certify_order_or_completion(self):
        r=eligibility(SITE,type(self.c));self.assertEqual(r['classification'],'full_rescan_required')
        self.assertIn('delivered_cookie_verification_hold',r['reason']);self.assertFalse(r['baseline_complete'])


if __name__=='__main__':unittest.main()
