import contextlib
import io
import unittest
from unittest.mock import patch
import requests
from crawler.sites import CRAWLERS
from delivery.incremental_status import eligibility
from tests.test_incremental_accc import DENIAL

SITE='arcep-fr-actualites'
CHASH='a'*32


def landing():
    return '<html><a href="/actualites/les-publications/gp-page/1.html?tx_gspublication_publicationlist%5Bfilters%5D%5Btype%5D=15&amp;cHash='+CHASH+'">Annual</a></html>'


def response(raw,status=200):
    r=requests.Response();r.status_code=status;r._content=raw.encode();r.encoding='utf-8';return r


class ArcepTests(unittest.TestCase):
    def setUp(self):
        self.c=CRAWLERS[SITE](None,delay=0);self.addCleanup(self.c._session.close)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def test_access_page_stops_without_browser_or_fallback_query(self):
        with patch.object(self.c._session,'get',return_value=response(DENIAL)) as get:
            self.assertEqual(self.c.crawl(),0);self.assertEqual(get.call_count,1)
            self.assertFalse(get.call_args.kwargs['allow_redirects'])
        self.assertEqual(self.c.delivery_source_failure,'authentication_failed');self.assertIsNone(self.c._pw_browser)

    def test_transport_failure_stops_without_hash_fallback(self):
        with patch.object(self.c._session,'get',side_effect=requests.Timeout) as get:
            self.assertEqual(self.c.crawl(),0);self.assertEqual(get.call_count,1)
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed')

    def test_unpublished_filter_hash_is_not_guessed(self):
        with patch.object(self.c._session,'get',return_value=response('<html>Ordinary content</html>')) as get:
            self.assertEqual(self.c.crawl(),0);self.assertEqual(get.call_count,1)
        self.assertEqual(self.c.delivery_source_failure,'invalid_response')

    def test_published_filter_hash_is_used(self):
        with patch.object(self.c._session,'get',return_value=response(landing())):
            self.assertEqual(self.c._get_chash(),CHASH)
        self.assertIsNone(self.c.delivery_source_failure)

    def test_filter_denial_preserves_authentication_reason(self):
        with patch.object(self.c._session,'get',side_effect=[response(landing()),response(DENIAL)]) as get:
            self.assertEqual(self.c.crawl(),0);self.assertEqual(get.call_count,2)
        self.assertEqual(self.c.delivery_source_failure,'authentication_failed')

    def test_redirect_not_automatically_followed(self):
        r=response('',302);r.headers['Location']='https://example.test'
        with patch.object(self.c._session,'get',return_value=r):self.assertIsNone(self.c._curl('https://example.test'))
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed')

    def test_hold_does_not_infer_order(self):
        r=eligibility(SITE,type(self.c));self.assertEqual(r['classification'],'full_rescan_required')
        self.assertIn('annual_filter_human_verification',r['reason']);self.assertFalse(r['baseline_complete'])


if __name__=='__main__':unittest.main()
