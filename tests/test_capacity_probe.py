import json
import unittest
from urllib.parse import parse_qs, urlsplit

from delivery.health.capacity_probe import parse_page, probe, query_url
from delivery.health.source_observer import HostGate


class CapacityProbeTest(unittest.TestCase):
    def contract(self):
        return {'site_id': 'fixture', 'kind': 'hal_solr', 'endpoint': 'https://example.org/search/',
                'query_pairs': [('q', '*'), ('fq', 'collection:EXAMPLE'), ('fq', 'type:REPORT')],
                'post_fetch_filter': 'abstract >= 100 chars'}

    def fetch(self, transform=None):
        def request(url, **kwargs):
            offset = int(parse_qs(urlsplit(url).query)['start'][0])
            data = {'response': {'numFound': 5, 'docs': [{'docid': offset}] if offset < 5 else []}}
            if transform:
                transform(offset, data)
            return {'status': 200, 'curl_exit': 0, 'body_complete': True,
                    'headers': {}, 'body': json.dumps(data).encode()}
        return request

    def test_filters_preserved_and_boundaries_never_certify_full_collection(self):
        c = self.contract()
        self.assertEqual(parse_qs(urlsplit(query_url(c, 3)).query)['fq'], ['collection:EXAMPLE', 'type:REPORT'])
        result = probe(c, HostGate(0), fetch=self.fetch())
        self.assertEqual(result['source_reported_total'], 5)
        self.assertEqual([r['offset'] for r in result['boundary_checks']], [0, 2, 4, 5])
        self.assertEqual(result['boundary_state'], 'sampled_boundaries_accessible_not_full_enumeration')
        self.assertIsNone(result['enumerable_total'])
        self.assertIsNone(result['save_eligible_total'])
        self.assertFalse(result['full_collection_verified'])

    def test_changing_source_is_not_accepted_as_stable_total(self):
        def change(offset, data):
            if offset:
                data['response']['numFound'] = 6
        result = probe(self.contract(), HostGate(0), fetch=self.fetch(change))
        self.assertEqual(result['boundary_state'], 'source_changed_between_observations')

    def test_repeated_page_ids_are_not_accepted_even_when_counts_match(self):
        def change(offset, data):
            if offset < 5:
                data['response']['docs'] = [{'docid': 0}]
        result = probe(self.contract(), HostGate(0), fetch=self.fetch(change))
        self.assertEqual(result['boundary_state'], 'repeated_ids_between_distinct_pages')

    def test_zero_total_remains_limited_to_query(self):
        def empty(offset, data):
            data['response'] = {'numFound': 0, 'docs': []}
        result = probe(self.contract(), HostGate(0), fetch=self.fetch(empty))
        self.assertEqual(result['boundary_state'], 'api_reported_empty_query_not_entire_site')
        self.assertFalse(result['full_collection_verified'])

    def test_api_total_does_not_hide_missing_last_page(self):
        def change(offset, data):
            if offset == 4:
                data['response']['docs'] = []
        result = probe(self.contract(), HostGate(0), fetch=self.fetch(change))
        self.assertEqual(result['boundary_state'], 'page_count_or_end_inconsistent')

    def test_boolean_total_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_page('hal_solr', b'{"response":{"numFound":true,"docs":[]}}')

    def test_unreviewed_response_schema_does_not_send_request(self):
        c = self.contract();c['kind'] = 'unreviewed_api'
        result = probe(c, HostGate(0), fetch=lambda *a, **kw: self.fail('unreviewed request'))
        self.assertEqual(result['boundary_state'], 'response_schema_requires_review')

    def test_doaj_uses_real_page_size_and_rejects_failed_deep_page(self):
        c = self.contract();c.update(kind='doaj', endpoint='https://example.org/articles/*')
        calls = []
        def fetch(url, **kwargs):
            query = parse_qs(urlsplit(url).query)
            self.assertEqual(query['pageSize'], ['50'])
            page = int(query['page'][0]);calls.append(page)
            if page > 1:
                return {'status': 422, 'curl_exit': 0, 'body_complete': True, 'headers': {}, 'body': b'{}'}
            body = json.dumps({'total': 10000, 'results': [{'id': str(i)} for i in range(50)]}).encode()
            return {'status': 200, 'curl_exit': 0, 'body_complete': True, 'headers': {}, 'body': body}
        result = probe(c, HostGate(0), fetch=fetch)
        self.assertEqual(calls, [1, 101])
        self.assertEqual(result['boundary_state'], 'boundary_access_unverified')
        self.assertEqual(result['source_reported_total'], 10000)
        self.assertIsNone(result['enumerable_total'])

    def test_ots_total_is_total_count_not_current_page_count(self):
        c = self.contract();c.update(kind='ots', query_pairs=[('query', ''), ('emittentId', '54')])
        def fetch(url, **kwargs):
            query = parse_qs(urlsplit(url).query)
            page = int(query['page'][0])
            self.assertEqual(query['emittentId'], ['54'])
            records = [{'key': str(page), 'emittentId': 54}] if page <= 5 else []
            data = {'error': None, 'meta': {'totalCount': '5', 'count': len(records), 'emittentId': '54', 'page': str(page)}, 'result': records}
            return {'status': 200, 'curl_exit': 0, 'body_complete': True, 'headers': {}, 'body': json.dumps(data).encode()}
        result = probe(c, HostGate(0), fetch=fetch)
        self.assertEqual(result['source_reported_total'], 5)
        self.assertEqual(result['boundary_state'], 'sampled_boundaries_accessible_not_full_enumeration')
        def reset_total(url, **kwargs):
            response = fetch(url, **kwargs)
            data = json.loads(response['body'])
            if not data['result']:
                data['meta']['totalCount'] = '0'
            response['body'] = json.dumps(data).encode()
            return response
        result = probe(c, HostGate(0), fetch=reset_total)
        self.assertEqual(result['boundary_state'], 'terminal_page_resets_reported_total_requires_review')

    def test_ots_error_count_and_total_are_validated(self):
        for data in ({'error': 'failure', 'meta': {'totalCount': '5', 'count': 0}, 'result': []},
                     {'meta': {'totalCount': True, 'count': 0}, 'result': []},
                     {'meta': {'totalCount': '5', 'count': 1}, 'result': []}):
            with self.assertRaises(ValueError):
                parse_page('ots', json.dumps(data).encode())


if __name__ == '__main__':
    unittest.main()
