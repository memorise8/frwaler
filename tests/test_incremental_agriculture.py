import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from crawler.sites import CRAWLERS
from delivery.incremental_status import eligibility
from tests.test_incremental_accc import DENIAL

SITE='agriculture-gouv-fr-recherche-developpem'
BODY='Controlled substantive publication text for request counting and native metadata regression. '


def listing():
    return '<div class="fr-card__body"><a class="fr-card__link" href="/research-document-one">Research one</a><span class="fr-card__date">1 octobre 2026</span></div>'


def detail(title):
    return f'<html><head><meta charset="utf-8"></head><body><h1>{title}</h1><main><article><p>{BODY}</p><a href="https://agriculture.gouv.fr/files/study.pdf">PDF</a></article></main></body></html>'


class AgricultureFailureTests(unittest.TestCase):
    def setUp(self):
        self.c=CRAWLERS[SITE](None,delay=0);self.addCleanup(self.c._session.close)
        self.g=self.c.crawl.__globals__;self.enterContext(patch('time.sleep'))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def test_error_status_and_partial_body_never_become_source(self):
        for code,exitcode in [('403',0),('429',0),('503',0),('200',28),('000',0)]:
            with self.subTest(code=code,exitcode=exitcode),patch.object(self.g['subprocess'],'run',return_value=SimpleNamespace(returncode=exitcode,stdout=f'body\n__HTTP_CODE__:{code}'.encode())) as curl:
                self.assertIsNone(self.g['_curl_get'](self.c.base_url));self.assertEqual(curl.call_count,4)

    def test_valid_response_keeps_native_text(self):
        with patch.object(self.g['subprocess'],'run',return_value=SimpleNamespace(returncode=0,stdout=b'body\n__HTTP_CODE__:200')):
            self.assertEqual(self.g['_curl_get'](self.c.base_url),'body\n')

    def test_list_failure_is_not_empty_success(self):
        with patch.dict(self.g,{'_curl_get':lambda url:None}):self.assertEqual(self.c.crawl(),0)
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed');self.assertFalse(self.c.delivery_exhausted)

    def test_failed_detail_stops_before_next_list(self):
        with patch.dict(self.g,{'_curl_get':lambda url:listing() if 'espace-presse' in url else None}),patch.object(self.c,'_save_paper',side_effect=AssertionError('missing body saved')):
            self.assertEqual(self.c.crawl(),0)
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed')

    def test_challenge_is_not_a_document_or_empty_list(self):
        for location in ('list','detail'):
            c=CRAWLERS[SITE](None,delay=0)
            try:
                with patch.dict(self.g,{'_curl_get':lambda url:DENIAL if location=='list' or 'espace-presse' not in url else listing()}):
                    self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'authentication_failed')
            finally:c._session.close()

    def test_scope_hold_does_not_certify_different_query_as_original(self):
        r=eligibility(SITE,type(self.c));self.assertEqual(r['classification'],'full_rescan_required')
        self.assertIn('original_scope_query_mismatch',r['reason']);self.assertFalse(r['baseline_complete'])


if __name__=='__main__':unittest.main()
