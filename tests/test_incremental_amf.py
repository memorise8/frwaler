import contextlib
import html
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from crawler.sites import CRAWLERS
from delivery.incremental_status import eligibility
from tests.test_incremental_accc import DENIAL

SITE='amf-france-org-fr'
BASE='https://www.amf-france.org'
BODY='Controlled substantive financial market publication content for native storage and request counting.'


def detail(title='Research'):
    return f'<html><head><meta charset="utf-8"></head><body><h1>{html.escape(title)}</h1><div class="contentToc">{BODY}</div></body></html>'


class AMFFailureTests(unittest.TestCase):
    def setUp(self):
        self.c=CRAWLERS[SITE](None,delay=0);self.addCleanup(self.c._session.close);self.g=self.c.crawl.__globals__
        self.enterContext(contextlib.redirect_stdout(io.StringIO()));self.enterContext(patch('time.sleep'))

    def test_nonzero_curl_exit_never_returns_partial_XML_or_error_body(self):
        def run(cmd,**kwargs):
            self.assertIn('-fsk',cmd);self.assertNotIn('-D',cmd)
            return SimpleNamespace(returncode=28,stdout=b'<urlset>partial',stderr=b'timeout')
        with patch.object(self.g['subprocess'],'run',side_effect=run) as get:
            self.assertEqual(self.g['_curl_get'](BASE),(None,None));self.assertEqual(get.call_count,3)

    def test_body_is_not_split_at_a_blank_line(self):
        body=b'<html>first\r\n\r\nsecond</html>'
        with patch.object(self.g['subprocess'],'run',return_value=SimpleNamespace(returncode=0,stdout=body)):
            self.assertEqual(self.g['_curl_get'](BASE),(body.decode(),{}))

    def test_missing_sitemap_is_failed(self):
        with patch.dict(self.g,{'_curl_get':lambda url:(None,None)}):self.assertEqual(self.c.crawl(),0)
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed')

    def test_unsupported_sitemap_index_is_not_empty_success(self):
        with patch.dict(self.g,{'_curl_get':lambda url:('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>',{})}):
            self.assertEqual(self.c.crawl(),0)
        self.assertEqual(self.c.delivery_source_failure,'invalid_response')

    def test_sitemap_challenge_is_failed(self):
        with patch.dict(self.g,{'_curl_get':lambda url:(DENIAL,{})}):self.assertEqual(self.c.crawl(),0)
        self.assertEqual(self.c.delivery_source_failure,'authentication_failed')

    def test_missing_detail_is_not_skipped(self):
        with patch.object(self.c,'_fetch_sitemap_urls',return_value=[(BASE+'/doc','2026-10-01')]),patch.dict(self.g,{'_curl_get':lambda url:(None,None)}):
            self.assertEqual(self.c.crawl(),0)
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed')

    def test_lastmod_cannot_invent_missing_publication_date(self):
        saved=[]
        def save(p):saved.append(p);self.c._last_save_created=True;return 1
        with patch.object(self.c,'_fetch_sitemap_urls',return_value=[(BASE+'/doc','2026-10-01')]), \
                patch.dict(self.g,{'_curl_get':lambda url:(detail(),{})}),patch.object(self.c,'_save_paper',side_effect=save):
            self.assertEqual(self.c.crawl(),1)
        self.assertIsNone(saved[0]['published_date']);self.assertEqual(saved[0]['listed_date'],'2026-10-01')

    def test_reviewed_scope_hold_never_certifies_baseline(self):
        r=eligibility(SITE,type(self.c));self.assertEqual(r['classification'],'full_rescan_required')
        self.assertIn('native_sitemap_scope_omits',r['reason']);self.assertFalse(r['baseline_complete'])


if __name__=='__main__':unittest.main()
