import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from crawler.sites import CRAWLERS
from delivery.incremental_status import eligibility
from tests.test_incremental_accc import DENIAL

SITE='anses-fr-fr'
BODY='Controlled abstract for PDF extraction transport and repeat request accounting. '*3


class AnsesTests(unittest.TestCase):
    def setUp(self):
        self.c=CRAWLERS[SITE](None,delay=0);self.addCleanup(self.c._session.close)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()));self.enterContext(patch('time.sleep'))

    def response(self,body,status='200',code=0):
        return SimpleNamespace(returncode=code,stdout=(body+'\n'+self.c._CURL_META_MARKER+status+'\t'+self.c.START_URL).encode(),stderr=b'controlled error')

    def test_transport_failure_not_empty_success(self):
        with patch.object(self.c.crawl.__globals__['subprocess'],'run',return_value=self.response('partial',code=28)) as get:
            self.assertEqual(self.c.crawl(),0);self.assertEqual(get.call_count,3)
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed')

    def test_missing_status_not_accepted(self):
        with patch.object(self.c.crawl.__globals__['subprocess'],'run',return_value=self.response('body',status='')) as get:
            self.assertEqual(self.c._curl_get_bytes(self.c.START_URL),(None,self.c.START_URL));self.assertEqual(get.call_count,3)
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed')

    def test_challenge_stops_before_parser(self):
        with patch.object(self.c.crawl.__globals__['subprocess'],'run',return_value=self.response(DENIAL,status='403')) as get:
            self.assertEqual(self.c.crawl(),0);self.assertEqual(get.call_count,1)
        self.assertEqual(self.c.delivery_source_failure,'authentication_failed')

    def test_parser_failure_is_explicit(self):
        with patch.object(self.c,'_curl_get_text',return_value=('body',self.c.START_URL)),patch.object(self.c,'_make_soup',return_value=None):
            self.assertEqual(self.c.crawl(),0)
        self.assertEqual(self.c.delivery_source_failure,'invalid_response')

    def test_pdf_fetch_failure_stops_before_next_item(self):
        with patch.object(self.c,'_curl_get_text',return_value=('body',self.c.START_URL)), \
                patch.object(self.c,'_parse_list',return_value=[{'pdf_url':'https://example.test/1.pdf'},{'pdf_url':'https://example.test/2.pdf'}]), \
                patch.object(self.c,'_curl_get_bytes',return_value=(None,'')) as get:
            self.assertEqual(self.c.crawl(),0);self.assertEqual(get.call_count,1)
        self.assertEqual(self.c.delivery_source_failure,'item_failed')

    def test_pdf_tool_failure_not_short_abstract_exclusion(self):
        with patch.object(self.c.crawl.__globals__['subprocess'],'run',return_value=SimpleNamespace(returncode=1,stdout=b'',stderr=b'bad PDF')) as get:
            self.assertEqual(self.c._extract_pdf_text(b'fixture'),'');self.assertEqual(get.call_count,2)
        self.assertEqual(self.c.delivery_source_failure,'item_failed')

    def test_successful_empty_pdf_text_is_distinct_from_tool_failure(self):
        with patch.object(self.c.crawl.__globals__['subprocess'],'run',return_value=SimpleNamespace(returncode=0,stdout=b'',stderr=b'')):
            self.assertEqual(self.c._extract_pdf_text(b'fixture'),'')
        self.assertIsNone(self.c.delivery_source_failure)

    def test_signature_and_online_dates_keep_distinct_roles(self):
        record=dict(external_id='a.pdf',title='Fixture',pdf_url='https://example.test/a.pdf',signature_date='2026-06-04',online_date='2026-09-09')
        r=self.c._parse_detail(record,BODY,record['pdf_url'])
        self.assertEqual(r['published_date'],'2026-06-04');self.assertEqual(r['metadata']['posted_date'],'2026-09-09')

    def test_reviewed_hold_does_not_guess_order(self):
        r=eligibility(SITE,type(self.c));self.assertEqual(r['classification'],'full_rescan_required')
        self.assertIn('sampled_signature_and_online_dates_nonmonotonic',r['reason']);self.assertFalse(r['baseline_complete'])


if __name__=='__main__':unittest.main()
