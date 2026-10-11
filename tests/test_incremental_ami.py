import contextlib
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from crawler.sites import CRAWLERS
from delivery.incremental_status import eligibility
from tests.test_incremental_accc import DENIAL

SITE='ami-swiss-en'
BODY='Controlled substantive publication abstract for external metadata request counting and date precision tests. '*2


class AMIFailureTests(unittest.TestCase):
    def setUp(self):
        self.c=CRAWLERS[SITE](None,delay=0);self.addCleanup(self.c._session.close)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()));self.enterContext(patch('time.sleep'))

    def test_partial_transport_response_is_rejected(self):
        def curl(cmd,**kwargs):
            self.assertIn('-fskL',cmd);return SimpleNamespace(returncode=28,stdout=b'partial',stderr=b'timeout')
        with patch.object(self.c.crawl.__globals__['subprocess'],'run',side_effect=curl) as get:
            self.assertIsNone(self.c._curl_get(self.c._LIST_URL));self.assertEqual(get.call_count,3)

    def test_failed_landing_is_not_empty_success(self):
        with patch.object(self.c,'_curl_get',return_value=None):self.assertEqual(self.c.crawl(),0)
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed')

    def test_failed_year_stops_before_next_year(self):
        with patch.object(self.c,'_get_years',return_value=['2026','2025']),patch.object(self.c,'_curl_get',return_value=None) as get:
            self.assertEqual(self.c.crawl(),0);self.assertEqual(get.call_count,1)
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed')

    def test_challenge_is_not_empty_year_menu(self):
        with patch.object(self.c,'_curl_get',return_value=DENIAL):self.assertEqual(self.c.crawl(),0)
        self.assertEqual(self.c.delivery_source_failure,'authentication_failed')

    def test_crossref_wrong_DOI_or_malformed_body_is_not_short_abstract(self):
        for raw in ('invalid','{"message":[]}',json.dumps({'message':{'DOI':'10.1/wrong','abstract':BODY}})):
            c=CRAWLERS[SITE](None,delay=0)
            try:
                with patch.object(c,'_curl_get',return_value=raw):self.assertEqual(c._fetch_crossref('10.1/right'),{})
                self.assertEqual(c.delivery_source_failure,'invalid_response')
            finally:c._session.close()

    def test_valid_no_abstract_remains_distinct_from_transport_failure(self):
        with patch.object(self.c,'_curl_get',return_value=json.dumps({'message':{'DOI':'10.1/RIGHT'}})):
            self.assertEqual(self.c._fetch_crossref('10.1/right'),{'DOI':'10.1/RIGHT'})
        self.assertIsNone(self.c.delivery_source_failure)

    def test_crossref_fetch_failure_is_explicit(self):
        with patch.object(self.c,'_curl_get',return_value=None):self.assertEqual(self.c._fetch_crossref('10.1/right'),{})
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed')

    def test_item_failure_does_not_skip_to_next_DOI(self):
        entry=dict(doi='10.1/right',url='https://doi.org/10.1/right',title='Title',year='2026')
        with patch.object(self.c,'_get_years',return_value=['2026']), \
                patch.object(self.c,'_curl_get',return_value='controlled'), \
                patch.object(self.c,'_parse_year_page',return_value=[entry,dict(entry,doi='10.1/next')]), \
                patch.object(self.c,'_fetch_crossref',side_effect=ValueError('invalid metadata')) as get:
            self.assertEqual(self.c.crawl(),0);self.assertEqual(get.call_count,1)
        self.assertEqual(self.c.delivery_source_failure,'item_failed')

    def test_year_order_hold_does_not_invent_newest_boundary(self):
        r=eligibility(SITE,type(self.c));self.assertEqual(r['classification'],'full_rescan_required')
        self.assertIn('year_only_source_groups',r['reason']);self.assertFalse(r['baseline_complete'])


if __name__=='__main__':unittest.main()
