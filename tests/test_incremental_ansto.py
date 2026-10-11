import contextlib
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from crawler.sites import CRAWLERS
from delivery.incremental_status import eligibility
from tests.test_incremental_accc import DENIAL

SITE='ansto-gov-au-search'
BODY='Controlled substantive abstract for native detail request accounting and absent publication date verification. '*2


def detail():
    return ('<html><head><script type="application/ld+json">'+json.dumps({'description':BODY})+
            '</script></head><body><a href="/sites/default/files/2020-05/fixture.pdf">PDF</a></body></html>')


class AnstoTests(unittest.TestCase):
    def setUp(self):
        self.c=CRAWLERS[SITE](None,delay=0);self.addCleanup(self.c._session.close);self.g=self.c.crawl.__globals__
        self.enterContext(contextlib.redirect_stdout(io.StringIO()));self.enterContext(patch('time.sleep'))

    def test_partial_curl_rejected(self):
        with patch.object(self.g['subprocess'],'run',return_value=SimpleNamespace(returncode=28,stdout=b'partial',stderr=b'')) as get:
            self.assertIsNone(self.g['_curl_get']('https://example.test'));self.assertEqual(get.call_count,3)
            self.assertIn('-fskL',get.call_args.args[0])

    def test_list_transport_failure_is_failed(self):
        with patch.dict(self.g,{'_curl_get':lambda *a,**k:None}):self.assertEqual(self.c.crawl(),0)
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed')

    def test_bad_ajax_shapes_are_not_source_end(self):
        for raw in ('invalid','{}','[null]','[]'):
            with patch.dict(self.g,{'_curl_get':lambda *a,**k:raw}),self.assertRaises(self.g['SourceFailure']) as caught:
                self.g['_fetch_search_page'](0)
            self.assertEqual(caught.exception.reason,'invalid_response')

    def test_challenge_not_listing_or_detail(self):
        for fn,arg in (('_fetch_search_page',0),('_fetch_detail','https://example.test')):
            with patch.dict(self.g,{'_curl_get':lambda *a,**k:DENIAL}),self.assertRaises(self.g['SourceFailure']) as caught:
                self.g[fn](arg)
            self.assertEqual(caught.exception.reason,'authentication_failed')

    def test_detail_failure_stops_before_next_item(self):
        def fetch(url):raise self.g['SourceFailure']('fetch_failed')
        with patch.dict(self.g,{'_fetch_search_page':lambda p:[{'title':'Fixture','url':'https://example.test'}],'_fetch_detail':fetch}):
            self.assertEqual(self.c.crawl(),0)
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed')

    def test_pdf_path_month_does_not_invent_publication_day(self):
        with patch.dict(self.g,{'_curl_get':lambda *a,**k:detail()}):r=self.g['_fetch_detail']('https://example.test')
        self.assertEqual(r['published_date'],'');self.assertEqual(r['node_id'],'')
        self.assertEqual(r['original_filename'],'fixture.pdf');self.assertEqual(r['abstract'],BODY.strip())

    def test_explicit_published_date_preserved(self):
        raw=detail().replace('"description":','"datePublished":"2025-04-17T10:00:00Z","description":')
        with patch.dict(self.g,{'_curl_get':lambda *a,**k:raw}):r=self.g['_fetch_detail']('https://example.test')
        self.assertEqual(r['published_date'],'2025-04-17')

    def test_hold_keeps_source_incomplete(self):
        r=eligibility(SITE,type(self.c));self.assertEqual(r['classification'],'full_rescan_required')
        self.assertIn('direct_PDF_media_omitted',r['reason']);self.assertFalse(r['baseline_complete'])


if __name__=='__main__':unittest.main()
