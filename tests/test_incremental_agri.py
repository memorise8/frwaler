import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from crawler.sites import CRAWLERS
from delivery.incremental_status import eligibility

SITE='agri-ee-ministeerium-uudised'


class AgriFailureTests(unittest.TestCase):
    def setUp(self):
        self.c=CRAWLERS[SITE](None,delay=0);self.addCleanup(self.c._session.close)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()));self.enterContext(patch('time.sleep'))

    def test_list_transport_failure_is_not_empty_source_success(self):
        with patch.object(self.c,'_curl_text',return_value=None):self.assertEqual(self.c.crawl(),0)
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed');self.assertFalse(self.c.delivery_exhausted)

    def test_challenge_never_reaches_parser(self):
        raw='<title>Just a moment...</title><script>window._cf_chl_opt={};</script>'
        with patch.object(self.c,'_curl_text',return_value=raw),patch.object(self.c,'_parse_records',side_effect=AssertionError('challenge parsed')):
            self.assertEqual(self.c.crawl(),0)
        self.assertEqual(self.c.delivery_source_failure,'authentication_failed')

    def test_article_mentioning_challenge_does_not_match(self):
        f=self.c.crawl.__globals__['_is_cf_challenge']
        self.assertFalse(f('<title>Research</title><main>Just a moment challenges.cloudflare.com</main>'))

    def test_failed_HEAD_does_not_save_or_skip_a_document(self):
        with patch.object(self.c,'_curl_text',return_value='<html/>'), \
                patch.object(self.c,'_parse_records',return_value=[{'item_url':'https://www.agri.ee/a.pdf'}]), \
                patch.object(self.c,'_curl_headers',return_value=None) as head, \
                patch.object(self.c,'_save_paper',side_effect=AssertionError('failed HEAD saved')):
            self.assertEqual(self.c.crawl(),0);self.assertEqual(head.call_count,1)
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed')

    def test_cffi_http_error_body_is_not_returned_as_source(self):
        g=self.c.crawl.__globals__;client=SimpleNamespace(get=lambda *a,**k:SimpleNamespace(status_code=503,text='<html>unavailable</html>'))
        with patch.dict(g,{'_cffi_requests':client}):
            self.assertIsNone(self.c._run_cffi(self.c.START_URL,headers={},context='test'))

    def test_curl_fallback_fails_for_error_status_including_HEAD(self):
        g=self.c.crawl.__globals__
        def call(cmd,**kwargs):
            self.assertTrue(any(a.startswith('-') and 'f' in a and 's' in a for a in cmd))
            return SimpleNamespace(stdout=b'',stderr=b'HTTP error',returncode=22)
        with patch.dict(g,{'_CFFI_AVAILABLE':False}),patch.object(g['subprocess'],'run',side_effect=call) as run:
            self.assertIsNone(self.c._curl_text(self.c.START_URL,context='test'))
            self.assertIsNone(self.c._curl_headers(self.c.START_URL,context='test'))
            self.assertEqual(run.call_count,6)

    def test_reviewed_hold_never_claims_full_history_baseline(self):
        r=eligibility(SITE,type(self.c));self.assertEqual(r['classification'],'full_rescan_required')
        self.assertIn('embedded_table_dates_nonmonotonic',r['reason']);self.assertFalse(r['baseline_complete'])


if __name__=='__main__':unittest.main()
