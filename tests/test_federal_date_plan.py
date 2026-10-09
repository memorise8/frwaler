import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from delivery.health.federal_date_plan import partition, query_url, run
from delivery.health.scaht_persistence import AuditStop


class FederalDatePlanTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name); self.output = self.root/'out'
        self.gate = Mock(); self.gate.enter.side_effect = lambda url: contextlib.nullcontext()
        self.enterContext(patch('delivery.health.federal_date_plan.RequestGate', return_value=self.gate))
        self.requests = []

    def fetch(self, url, **kwargs):
        self.requests.append(url)
        params = parse_qs(urlsplit(url).query)
        self.assertEqual(params['conditions[agencies][]'], ['health-and-human-services-department'])
        self.assertEqual(params['conditions[search_type_id]'], ['3'])
        self.assertEqual(params['per_page'], ['200'])
        docs = [{'document_number': f'id-{n}', 'publication_date': f'2025-01-0{n}'} for n in range(1, 5)]
        lo = params.get('conditions[publication_date][gte]', ['0001-01-01'])[0]
        hi = params.get('conditions[publication_date][lte]', ['9999-12-31'])[0]
        docs = [d for d in docs if lo <= d['publication_date'] <= hi]
        if params['order'] == ['newest']: docs.reverse()
        return self.response({'count': min(len(docs), 3), 'results': docs[:1]})

    @staticmethod
    def response(data, status=200):
        return {'status': status, 'curl_exit': 0, 'body_complete': True, 'headers': {}, 'body': json.dumps(data).encode()}

    def test_ranges_are_disjoint_and_leaf_sum_can_exceed_clipped_root_count(self):
        report = run(self.output, self.root/'gate', leaf_limit=3, fetch=self.fetch)
        self.assertEqual(report['state'], 'disjoint_date_range_counts_observed')
        self.assertEqual(report['nodes'][0]['reported_count'], 3)
        self.assertEqual(report['reported_leaf_count_sum'], 4)
        self.assertEqual([(r['lower'], r['upper']) for r in report['leaves']],
                         [('2025-01-01', '2025-01-02'), ('2025-01-03', '2025-01-04')])
        self.assertFalse(report['source_id_enumeration_verified'])
        self.assertFalse(report['full_collection_verified'])

    def test_budget_checkpoint_resumes_without_repeating_completed_ranges(self):
        first = run(self.output, self.root/'gate', leaf_limit=3, request_budget=3, fetch=self.fetch)
        self.assertEqual(first['state'], 'checkpointed')
        self.assertEqual(len(self.requests), 3)
        second = run(self.output, self.root/'gate', leaf_limit=3, resume=True, fetch=self.fetch)
        self.assertEqual(second['state'], 'disjoint_date_range_counts_observed')
        self.assertEqual(len(self.requests), 5)
        self.assertEqual(len(set(self.requests)), 5)

    def test_altered_evidence_blocks_resume_before_network(self):
        report = run(self.output, self.root/'gate', leaf_limit=3, request_budget=1, fetch=self.fetch)
        (self.output/report['responses'][0]['evidence']).write_text('{}')
        with self.assertRaises(ValueError):
            run(self.output, self.root/'gate', leaf_limit=3, resume=True, fetch=self.fetch)
        self.assertEqual(len(self.requests), 1)

    def test_ignored_date_filters_are_rejected_and_body_preserved(self):
        def ignored(url, **kwargs):
            if 'publication_date' in url:
                return self.response({'count': 1, 'results': [{'document_number': 'bad', 'publication_date': '2030-01-01'}]})
            return self.fetch(url, **kwargs)
        report = run(self.output, self.root/'gate', leaf_limit=3, fetch=ignored)
        self.assertEqual(report['reason'], 'date_filter_not_honored')
        self.assertTrue((self.output/report['responses'][-1]['evidence']).exists())

    def test_denied_http_stops_after_one_request_and_preserves_response(self):
        fetch = Mock(return_value=self.response({'error': 'denied'}, status=403))
        report = run(self.output, self.root/'gate', fetch=fetch)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(report['reason'], 'source_http_or_body_incomplete')
        self.assertEqual(report['responses'][0]['http_status'], 403)

    def test_malformed_http200_is_preserved(self):
        report = run(self.output, self.root/'gate', fetch=lambda *a, **k: self.response({'message': 'unexpected'}))
        self.assertEqual(report['reason'], 'source_schema_invalid')
        self.assertEqual(len(report['responses']), 1)
        self.assertEqual(json.loads((self.output/report['responses'][0]['evidence']).read_text()), {'message': 'unexpected'})

    def test_rate_limit_records_shared_cooldown(self):
        report = run(self.output, self.root/'gate', fetch=lambda *a, **k: self.response({}, status=429))
        self.assertEqual(report['state'], 'host_cooldown_deferred')
        self.gate.defer.assert_called_once()

    def test_multiple_sample_records_are_valid_with_short_server_window(self):
        def multiple(url, **kwargs):
            result = self.fetch(url, **kwargs)
            data = json.loads(result['body'])
            if 'publication_date' not in url:
                dates = ('2025-01-01', '2025-01-02') if 'order=oldest' in url else ('2025-01-04', '2025-01-03')
                data['results'] = [{'document_number': d, 'publication_date': d} for d in dates]
            return self.response(data)
        report = run(self.output, self.root/'gate', leaf_limit=3, fetch=multiple)
        self.assertEqual(report['state'], 'disjoint_date_range_counts_observed')

    def test_wrong_oldest_order_cannot_define_date_bounds(self):
        data = {'count': 2, 'results': [{'document_number': 'a', 'publication_date': '2025-01-02'},
                                       {'document_number': 'b', 'publication_date': '2025-01-01'}]}
        report = run(self.output, self.root/'gate', fetch=lambda *a, **k: self.response(data))
        self.assertEqual(report['reason'], 'date_order_not_honored')
        self.assertEqual(report['bounds'], {})

    def test_single_day_limit_never_claims_full_scope(self):
        with self.assertRaises(AuditStop): partition('2025-01-01', '2025-01-01')

    def test_leap_day_partition_has_no_gap(self):
        self.assertEqual(partition('2024-02-28', '2024-03-01'),
                         [{'lower': '2024-02-28', 'upper': '2024-02-29'}, {'lower': '2024-03-01', 'upper': '2024-03-01'}])
