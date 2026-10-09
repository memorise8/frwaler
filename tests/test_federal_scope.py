import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from delivery.health.federal_scope import run


class FederalScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.calls = []; self.mode = None
        self.gate = Mock(); self.gate.enter.side_effect = lambda url: contextlib.nullcontext()
        self.enterContext(patch('delivery.health.federal_scope.RequestGate', return_value=self.gate))

    def fetch(self, url, **kwargs):
        query = {k:v[0] for k,v in parse_qs(urlsplit(url).query).items()}; page = int(query['page'])
        self.calls.append(page)
        if page > 1: self.assertEqual(query['search_after_cursor'], f'cursor-{page}')
        records = [{'document_number': str(n), 'title': f'Research {n}', 'html_url': f'https://example.test/{n}',
                    'abstract': 'Research evidence. '*10} for n in range((page-1)*2, page*2)]
        if page == 2: records[0]['abstract'] = 'short'
        if self.mode == 'duplicate' and page == 2: records[0]['document_number'] = '0'
        nxt = dict(query, page=str(page+1), search_after_cursor=f'cursor-{page+1}', format='json')
        link = url.split('?')[0]+'?'+urlencode(nxt) if page < 3 else None
        if self.mode == 'premature': link = None
        if self.mode == 'scope':
            nxt['conditions[agencies][]'] = 'other-agency'; link = url.split('?')[0]+'?'+urlencode(nxt)
        status = 429 if self.mode == 'rate' else 403 if self.mode == 'denied' else 200
        return {'status': status, 'curl_exit': 0, 'body_complete': True, 'headers': {'retry-after': '120'},
                'body': json.dumps({'count': 10 if self.mode == 'premature' else 2, 'total_pages': 1, 'results': records, 'next_page_url': link}).encode()}

    def audit(self, **kwargs):
        return run(self.root/'out', self.root/'gate', fetch=self.fetch, **kwargs)

    def test_checkpoint_resume_passes_display_count_and_preserves_exclusions(self):
        first = self.audit(page_budget=1)
        self.assertEqual(first['state'], 'checkpointed'); self.assertEqual(first['observed_unique_ids'], 2)
        second = self.audit(resume=True)
        self.assertEqual(self.calls, [1, 2, 3])
        self.assertEqual(second['state'], 'provider_cursor_terminal_observed')
        self.assertEqual(second['first_reported_count'], 2)
        self.assertEqual(second['observed_unique_ids'], 6); self.assertEqual(second['observed_code_eligible'], 5)
        manifest = json.loads((self.root/'out/manifest.json').read_text())
        self.assertEqual([r['document_number'] for r in manifest], list(map(str, range(6))))
        self.assertFalse(second['full_collection_verified']); self.assertFalse(second['exclusion_policy_accepted'])

    def test_changed_evidence_rejects_resume_before_network(self):
        first = self.audit(page_budget=1)
        (self.root/'out'/first['pages'][0]['evidence']).write_bytes(b'changed')
        with self.assertRaises(ValueError): self.audit(resume=True)
        self.assertEqual(self.calls, [1])

    def test_duplicate_source_ids_never_certify_terminal(self):
        self.mode = 'duplicate'; result = self.audit()
        self.assertEqual(result['reason'], 'repeated_source_identifiers')
        self.assertEqual(result['observed_unique_ids'], 2)
        self.assertFalse((self.root/'out/manifest.json').exists())

    def test_changed_query_link_stops_before_following(self):
        self.mode = 'scope'; result = self.audit()
        self.assertEqual(result['state'], 'incomplete'); self.assertEqual(self.calls, [1])

    def test_access_denial_and_rate_limit_stop_without_more_requests(self):
        self.mode = 'denied'; result = self.audit()
        self.assertEqual(result['failed_request']['http_status'], 403); self.assertEqual(self.calls, [1])

    def test_rate_limit_persists_shared_cooldown(self):
        self.mode = 'rate'; result = self.audit()
        self.assertEqual(result['state'], 'host_cooldown_deferred')
        self.gate.defer.assert_called_once(); self.assertEqual(self.calls, [1])

    def test_missing_next_link_before_advertised_count_is_incomplete(self):
        self.mode = 'premature'; result = self.audit()
        self.assertEqual(result['reason'], 'cursor_terminal_before_reported_lower_bound')
        self.assertFalse((self.root/'out/manifest.json').exists())

    def range_fetch(self, url, **kwargs):
        response = self.fetch(url, **kwargs); data = json.loads(response['body'])
        data['count'] = 6
        for doc in data['results']: doc['publication_date'] = '2025-01-01'
        if data['next_page_url'] is None: data.pop('next_page_url')
        response['body'] = json.dumps(data).encode()
        return response

    def range_audit(self, **kwargs):
        return run(self.root/'out', self.root/'gate',
                   date_range={'lower': '2025-01-01', 'upper': '2025-01-02', 'expected_count': 6}, **kwargs)

    def test_bounded_date_range_accepts_omitted_terminal_link_only_at_expected_count(self):
        result = self.range_audit(fetch=self.range_fetch)
        self.assertEqual(result['state'], 'provider_cursor_terminal_observed')
        self.assertEqual(result['observed_unique_ids'], 6)
        self.assertFalse(result['full_collection_verified'])

    def test_date_range_checkpoint_keeps_exact_filter_and_expected_count(self):
        first = self.range_audit(fetch=self.range_fetch, page_budget=1)
        self.assertEqual(first['state'], 'checkpointed')
        final = self.range_audit(fetch=self.range_fetch, resume=True)
        self.assertEqual(final['state'], 'provider_cursor_terminal_observed')
        self.assertEqual(self.calls, [1, 2, 3])

    def test_date_range_rejects_outside_dates(self):
        def bad(url, **kwargs):
            response = self.range_fetch(url, **kwargs); data = json.loads(response['body'])
            data['results'][0]['publication_date'] = '2024-01-01'
            response['body'] = json.dumps(data).encode(); return response
        result = self.range_audit(fetch=bad)
        self.assertEqual(result['reason'], 'date_filter_not_honored')

    def test_changed_date_filter_in_next_link_is_rejected_before_request(self):
        def bad(url, **kwargs):
            response = self.range_fetch(url, **kwargs); data = json.loads(response['body'])
            data['next_page_url'] = data['next_page_url'].replace('2025-01-01', '2024-01-01')
            response['body'] = json.dumps(data).encode(); return response
        result = self.range_audit(fetch=bad)
        self.assertEqual(result['reason'], 'continuation_changes_date_range')
        self.assertEqual(self.calls, [1])

    def test_date_range_premature_terminal_is_not_complete(self):
        self.mode = 'premature'
        result = self.range_audit(fetch=self.range_fetch)
        self.assertEqual(result['reason'], 'date_range_terminal_count_mismatch')

    def test_date_range_count_change_stops_enumeration(self):
        result = self.range_audit(fetch=self.fetch)
        self.assertEqual(result['reason'], 'date_range_count_changed')

    def test_schema_error_response_is_saved_for_review(self):
        def bad(url, **kwargs):
            response = self.fetch(url, **kwargs); response['body'] = b'{"message":"unexpected"}'
            return response
        result = run(self.root/'out', self.root/'gate', fetch=bad)
        self.assertEqual(result['reason'], 'source_schema_invalid')
        self.assertEqual((self.root/'out'/result['last_response']['evidence']).read_bytes(), b'{"message":"unexpected"}')


if __name__ == '__main__':
    unittest.main()
