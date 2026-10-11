import contextlib
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit
import xml.etree.ElementTree as ET

from crawler.sites import CRAWLERS
from delivery.incremental_status import eligibility
from tests.test_incremental_accc import DENIAL

SITE='arrow-tudublin-ie-creaart'
FIXTURE=Path(__file__).with_name('fixtures')/'arrow_new_saves/page-0.xml'


class ArrowFailureTests(unittest.TestCase):
    def setUp(self):
        self.c=CRAWLERS[SITE](None,delay=0);self.addCleanup(self.c._session.close)
        self.g=self.c.crawl.__globals__;self.ns=self.g['_NS'];self.raw=FIXTURE.read_bytes()
        self.enterContext(contextlib.redirect_stdout(io.StringIO()));self.enterContext(patch('time.sleep'))
        self.enterContext(patch.object(self.c,'_save_paper',side_effect=self.save))
        self.saved=[]
    def save(self,paper):self.saved.append(paper);self.c._last_save_created=True
    def call(self,*bodies):
        with patch.dict(self.g,{'_curl_get':unittest.mock.Mock(side_effect=bodies)}):return self.c.crawl()

    def test_transport_error_not_accepted_as_source(self):
        with patch.object(self.g['subprocess'],'run',return_value=SimpleNamespace(returncode=22,stdout=self.raw)) as run:
            self.assertEqual(self.c.crawl(),0);self.assertEqual(run.call_count,3)
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed')

    def test_access_response_has_its_own_failure_reason(self):
        self.assertEqual(self.call(DENIAL.encode()),0);self.assertEqual(self.c.delivery_source_failure,'authentication_failed')

    def test_malformed_XML_is_not_an_empty_success(self):
        self.assertEqual(self.call(b'<broken'),0);self.assertEqual(self.c.delivery_source_failure,'invalid_response')

    def test_OAI_error_is_not_source_exhaustion(self):
        root=ET.fromstring(self.raw);lr=root.find('oai:ListRecords',self.ns);root.remove(lr)
        ET.SubElement(root,'{'+self.ns['oai']+'}error',code='badResumptionToken').text='expired'
        self.assertEqual(self.call(ET.tostring(root)),0);self.assertEqual(self.c.delivery_source_failure,'invalid_response')

    def test_wrong_set_request_echo_is_rejected(self):
        root=ET.fromstring(self.raw);root.find('oai:request',self.ns).set('set','publication:other')
        self.assertEqual(self.call(ET.tostring(root)),0);self.assertEqual(self.c.delivery_source_failure,'invalid_response')

    def test_missing_record_identity_is_not_silently_excluded(self):
        record=ET.fromstring(self.raw).find('oai:ListRecords/oai:record',self.ns);header=record.find('oai:header',self.ns)
        header.remove(header.find('oai:identifier',self.ns))
        self.assertFalse(self.c._process_record(record,set()));self.assertEqual(self.c.delivery_source_failure,'invalid_response')

    def test_native_empty_abstract_is_an_exclusion_not_a_failure(self):
        record=ET.fromstring(self.raw).find('oai:ListRecords/oai:record',self.ns)
        record.find('oai:metadata/oai_dc:dc/dc:description',self.ns).text=''
        self.assertFalse(self.c._process_record(record,set()));self.assertIsNone(self.c.delivery_source_failure)

    def test_real_failure_API_accepts_repeated_token_reason(self):
        first=ET.fromstring(self.raw);token=first.find('oai:ListRecords/oai:resumptionToken',self.ns).text
        second=ET.fromstring(self.raw);request=second.find('oai:request',self.ns);request.attrib.clear()
        request.attrib.update(verb='ListRecords',resumptionToken=token)
        self.assertEqual(self.call(ET.tostring(first),ET.tostring(second)),3)
        self.assertEqual(self.c.delivery_source_failure,'invalid_response')

    def test_opaque_token_is_encoded_as_one_query_argument(self):
        root=ET.fromstring(self.raw);root.find('oai:ListRecords/oai:resumptionToken',self.ns).text='token+literal&suffix/100//'
        calls=[]
        def get(url):calls.append(url);return ET.tostring(root) if len(calls)==1 else None
        with patch.dict(self.g,{'_curl_get':get}):self.assertEqual(self.c.crawl(),3)
        self.assertEqual(parse_qs(urlsplit(calls[1]).query),{'verb':['ListRecords'],'resumptionToken':['token+literal&suffix/100//']})

    def test_save_failure_stops_before_following_record(self):
        with patch.object(self.c,'_save_paper',side_effect=ValueError('controlled storage failure')) as save:
            self.assertEqual(self.call(self.raw),0);self.assertEqual(save.call_count,1)
        self.assertEqual(self.c.delivery_source_failure,'item_failed')

    def test_reviewed_unsorted_source_hold(self):
        r=eligibility(SITE,type(self.c));self.assertEqual(r['classification'],'full_rescan_required')
        self.assertIn('nonmonotonic',r['reason']);self.assertFalse(r['baseline_complete'])


if __name__=='__main__':unittest.main()
