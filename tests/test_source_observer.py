import json
import gzip
import hashlib
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import unittest
from unittest.mock import patch

from delivery.health.scope_inventory import public_url, static_profile
from delivery.health.source_observer import HostGate, HostDeferred, inspect_body, observe, public_address, retry_after


class SourceInventoryTest(unittest.TestCase):
    def test_static_scope_preserves_board_filter_but_does_not_claim_total(self):
        p = static_profile('', config={'base_url': 'https://example.org', 'list_page': {
            'url': 'https://example.org/list', 'params': {'board': 23}, 'pagination': {'param': 'page'}}})
        self.assertEqual(p['selected_entry']['url'], 'https://example.org/list?board=23')
        self.assertIsNone(p['current_total'])
        self.assertEqual(p['current_total_quality'], 'unknown')

    def test_missing_config_and_bad_selector_are_distinct_findings(self):
        p = static_profile('', config={'crawl_type': 'single-page', 'base_url': 'https://example.org'})
        self.assertIn('missing_list_page_required_by_generic_crawler', p['config_issues'])
        p = static_profile('', config={'list_page': {'url': 'https://example.org', 'selectors': {'title': 'div['}}})
        self.assertIn('invalid_css_selector:list_page.title', p['config_issues'])

    def test_static_expressions_are_not_executed(self):
        p = static_profile("_BASE_URL='https://example.org'\n_LIST_URL=_BASE_URL+'/list'\nBAD=__import__('os').system('exit 9')")
        self.assertEqual(p['selected_entry']['url'], 'https://example.org/list')

    def test_credentials_and_unresolved_template_are_not_public_entries(self):
        for url in ['https://example.org/?api_key=secret', 'https://user:pass@example.org',
                    'https://example.org/?sessionid=secret', 'file:///etc/passwd', 'https://example.org/{page}']:
            self.assertIsNone(public_url(url))


class SourceObserverTest(unittest.TestCase):
    def site(self):
        return {'site_id': 'fixture', 'selected_entry': {'role': 'config.list_page.url', 'url': 'https://example.org/list'}}

    def response(self, status=200, body=b'{"total":12}', headers=None):
        return {'status': status, 'curl_exit': 0, 'body_complete': True, 'body': body,
                'headers': headers or {'content-type': 'application/json'}}

    def test_response_total_remains_candidate_and_secret_body_is_not_stored(self):
        r = observe(self.site(), HostGate(0), fetch=lambda *a, **kw: self.response(body=b'{"total":12,"token":"secret"}'))
        self.assertEqual(r['body_observation']['count_candidates'][0]['value'], 12)
        self.assertIsNone(r['current_total'])
        self.assertEqual(r['persistence'], 'not_tested')
        self.assertNotIn('secret', json.dumps(r))

    def test_zero_is_not_proof_of_empty_scope(self):
        r = inspect_body(b'{"total":0}', 'application/json')
        self.assertEqual(r['count_candidates'][0]['value'], 0)
        self.assertIsNone(r['document_count'])

    def test_redirect_destination_requires_review_if_credentials_present(self):
        r = observe(self.site(), HostGate(0), fetch=lambda *a, **kw: self.response(302, headers={'location': '/?token=secret'}))
        self.assertEqual(r['state'], 'redirect_requires_review')
        self.assertNotIn('secret', json.dumps(r))

    def test_rate_limit_defers_same_host_without_second_request(self):
        gate = HostGate(0)
        fetch = lambda *a, **kw: self.response(429, headers={'retry-after': '3600'})
        first = observe(self.site(), gate, fetch=fetch)
        second = observe(self.site(), gate, fetch=lambda *a, **kw: self.fail('request during cooldown'))
        self.assertEqual(first['state'], 'rate_limited')
        self.assertEqual(second['state'], 'host_cooldown_deferred')

    def test_host_lock_covers_entire_body_fetch(self):
        gate = HostGate(0)
        active = 0
        maximum = 0
        lock = threading.Lock()
        def fetch(*a, **kw):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(.02)
            with lock:
                active -= 1
            return self.response()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: observe(self.site(), gate, fetch=fetch), range(8)))
        self.assertEqual(maximum, 1)
        self.assertEqual(len(results), 8)

    def test_redirect_obeys_destination_cooldown(self):
        gate = HostGate(0)
        gate.defer('other.example.org', '3600')
        r = observe(self.site(), gate, fetch=lambda *a, **kw: self.response(302, headers={'location': 'https://other.example.org/list'}))
        self.assertEqual(r['state'], 'host_cooldown_deferred')
        self.assertEqual(len(r['hops']), 1)

    def test_private_dns_results_are_rejected(self):
        with patch('delivery.health.source_observer.subprocess.run') as run:
            run.return_value.stdout = b'["127.0.0.1"]'
            with self.assertRaises(ValueError):
                public_address('example.org', 443)

    def test_numeric_and_http_date_retry_after(self):
        self.assertEqual(retry_after('30', 0), 30)
        self.assertEqual(retry_after('Thu, 01 Jan 1970 00:01:00 GMT', 0), 60)

    def test_exception_messages_are_not_recorded(self):
        def fetch(*a, **kw):
            raise RuntimeError('credential-value')
        r = observe(self.site(), HostGate(0), fetch=fetch)
        self.assertEqual(r['exception_type'], 'RuntimeError')
        self.assertNotIn('credential-value', json.dumps(r))

    def test_partial_body_is_not_used_for_count_extraction(self):
        response = self.response()
        response.update(curl_exit=63, body_complete=False)
        r = observe(self.site(), HostGate(0), fetch=lambda *a, **kw: response)
        self.assertEqual(r['state'], 'body_limit')
        self.assertNotIn('body_observation', r)

    def test_unsolicited_gzip_is_decoded_without_losing_wire_hash(self):
        source = b'{"total":27,"token":"not-persisted"}'
        wire = gzip.compress(source)
        for encoding in (None, 'gzip', 'GZIP', 'x-gzip'):
            with self.subTest(encoding=encoding):
                headers = {'content-type': 'application/json'}
                if encoding: headers['content-encoding'] = encoding
                r = observe(self.site(), HostGate(0), fetch=lambda *a, **kw:self.response(body=wire,headers=headers))
                self.assertEqual(r['body_observation']['count_candidates'][0]['value'],27)
                self.assertEqual(r['body_sha256'],hashlib.sha256(wire).hexdigest())
                self.assertEqual(r['decoded_body_sha256'],hashlib.sha256(source).hexdigest())
                self.assertIsNone(r['current_total'])
                self.assertNotIn('not-persisted',json.dumps(r))

    def test_expanded_gzip_body_cannot_exceed_observation_budget(self):
        wire = gzip.compress(b'X' * 10000)
        with patch('delivery.health.source_observer.MAX_BODY',64):
            r = observe(self.site(), HostGate(0), fetch=lambda *a, **kw:self.response(body=wire))
        self.assertEqual(r['state'],'decoded_body_limit')
        self.assertNotIn('body_observation',r)

    def test_corrupt_and_unsupported_encoded_bodies_are_not_parsed(self):
        cases = [(b'not gzip','gzip','invalid_compressed_body'),
                 (gzip.compress(b'{"total":8}')[:-4],'gzip','invalid_compressed_body'),
                 (b'{"total":8}','br','unsupported_content_encoding')]
        for body,encoding,state in cases:
            with self.subTest(state=state):
                r=observe(self.site(),HostGate(0),fetch=lambda *a,**kw:self.response(body=body,headers={'content-type':'application/json','content-encoding':encoding}))
                self.assertEqual(r['state'],state)
                self.assertNotIn('body_observation',r)

    def test_incomplete_body_without_curl_error_is_not_success(self):
        response=self.response();response['body_complete']=False
        r=observe(self.site(),HostGate(0),fetch=lambda *a,**kw:response)
        self.assertEqual(r['state'],'body_limit')
        self.assertNotIn('body_observation',r)


if __name__ == '__main__':
    unittest.main()
