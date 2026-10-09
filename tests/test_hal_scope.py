import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from delivery.health.hal_scope import enumerate_query


class HalScopeTests(unittest.TestCase):
    def case(self, *, duplicate=False, early_end=False, total_changes=False, status=200):
        contract = {'site_id': 'hal-fixture', 'endpoint': 'https://api.archives-ouvertes.fr/search/',
                    'query_pairs': [('q', '*'), ('fq', 'collCode_s:TEST'), ('sort', 'submittedDate_tdate desc,docid asc')]}
        gate = Mock(); gate.enter.side_effect = lambda url: contextlib.nullcontext()
        offsets = []
        def fetch(url, **kwargs):
            query = parse_qs(urlsplit(url).query); offset = int(query['start'][0]); offsets.append(offset)
            # Simulate a server returning fewer records than requested.
            docs = [{'docid': str(n), 'halId_s': f'hal-{n}', 'uri_s': f'https://hal.science/hal-{n}'}
                    for n in range(offset+1, min(offset+3, 6))]
            if offset and duplicate: docs[0]['docid'] = '1'
            if offset and early_end: docs = []
            total = 6 if offset and total_changes else 5
            body = json.dumps({'response': {'numFound': total, 'docs': docs}}).encode()
            return {'status': status, 'curl_exit': 0, 'body_complete': True,
                    'headers': {'retry-after': '120'}, 'body': body}
        with tempfile.TemporaryDirectory() as directory:
            result = enumerate_query(contract, Path(directory)/'audit', gate, fetch=fetch)
            manifest_path = Path(directory)/'audit/manifest.json'
            manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
        return result, manifest, offsets, gate

    def test_all_unique_ids_and_empty_terminal_survive_short_windows(self):
        result, manifest, offsets, _ = self.case()
        self.assertEqual(result['state'], 'observed_query_ids_enumerated')
        self.assertEqual(offsets, [0, 2, 4, 5])
        self.assertEqual([r['docid'] for r in manifest], ['1', '2', '3', '4', '5'])
        self.assertFalse(result['full_collection_verified'])
        self.assertFalse(result['database_persistence_verified'])

    def test_overlap_does_not_pass_reported_total(self):
        result, manifest, _, _ = self.case(duplicate=True)
        self.assertEqual(result['reason'], 'missing_or_repeated_source_identifiers')
        self.assertIsNone(manifest)

    def test_early_terminal_and_changing_total_are_not_complete(self):
        for options, reason in (({'early_end': True}, 'terminal_page_before_reported_total'),
                                ({'total_changes': True}, 'source_total_changed_during_enumeration')):
            with self.subTest(options=options):
                result, manifest, _, _ = self.case(**options)
                self.assertEqual(result['reason'], reason); self.assertIsNone(manifest)

    def test_rate_limit_defers_without_more_requests(self):
        result, _, offsets, gate = self.case(status=429)
        self.assertEqual(result['state'], 'host_cooldown_deferred')
        self.assertEqual(offsets, [0]); gate.defer.assert_called_once()

    def test_access_denial_is_not_empty_source(self):
        result, manifest, offsets, _ = self.case(status=403)
        self.assertEqual(result['state'], 'incomplete')
        self.assertIsNone(result['source_reported_total']); self.assertIsNone(manifest)
        self.assertEqual(offsets, [0])


if __name__ == '__main__':
    unittest.main()
