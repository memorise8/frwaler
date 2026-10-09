import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from delivery.health.federal_persistence import run


class FederalPilotTests(unittest.TestCase):
    def case(self, *, status=200, change=False):
        gate = Mock(); gate.enter.side_effect = lambda url: contextlib.nullcontext()
        calls = []
        def send(adapter, request, **kwargs):
            calls.append(request.url)
            docs = [{'document_number': str(n), 'html_url': f'https://example.test/{n}',
                     'title': f'Research {n}', 'abstract': 'Scientific document. '*10,
                     'publication_date': '2026-01-05'} for n in range(20)]
            if change and len(calls) > 1: docs[0]['title'] = 'Updated title'
            response = requests.Response(); response.status_code = status
            response.url = request.url; response.request = request
            response.raw = io.BytesIO(json.dumps({'results': docs, 'count': 1000, 'total_pages': 5}).encode())
            response.headers['Retry-After'] = '120'
            return response
        with tempfile.TemporaryDirectory() as directory:
            with patch('delivery.health.federal_persistence.RequestGate', return_value=gate), \
                    patch('delivery.health.file_audit.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('8.8.8.8', 443))]), \
                    patch.object(requests.adapters.HTTPAdapter, 'send', send), patch('time.sleep'), \
                    contextlib.redirect_stdout(io.StringIO()):
                result = run(Path(directory)/'out', Path(directory)/'gate')
        return result, calls, gate

    def test_native_adapter_real_save_cancel_reopen_and_prefix_identity(self):
        result, calls, _ = self.case()
        self.assertEqual(result['state'], 'bounded_native_save_cancel_reopen_verified')
        self.assertEqual([p['actual_new'] for p in result['phases']], [3, 3, 2, 3])
        self.assertEqual(result['phases'][2]['state'], 'cancelled')
        self.assertEqual(result['stored_count'], 11); self.assertEqual(len(calls), 4)
        self.assertFalse(result['full_collection_verified']); self.assertFalse(result['publisher_files_verified'])

    def test_changed_source_preserves_already_saved_phase(self):
        result, calls, _ = self.case(change=True)
        self.assertEqual(result['reason'], 'source_changed_between_pilot_runs')
        self.assertEqual([p['actual_new'] for p in result['phases']], [3, 0])
        self.assertEqual(len(calls), 2)

    def test_denial_and_rate_limit_stop_before_any_save_or_retry(self):
        for status, state in ((403, 'incomplete'), (429, 'host_cooldown_deferred')):
            with self.subTest(status=status):
                result, calls, gate = self.case(status=status)
                self.assertEqual(result['state'], state)
                self.assertEqual(result['phases'][0]['actual_new'], 0); self.assertEqual(len(calls), 1)
                if status == 429: gate.defer.assert_called_once()


if __name__ == '__main__':
    unittest.main()
