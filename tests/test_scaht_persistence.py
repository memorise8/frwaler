import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import requests

from delivery.health.scaht_persistence import run_pilot
from tests.test_scaht_scope import page


class NativePilotTests(unittest.TestCase):
    def run_case(self, status=200):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root/'listing.json'
            evidence.write_text(json.dumps({'passes': [{'records': [{'external_id': f'doc-{n}'} for n in range(20)]}]}))
            gate = Mock()
            gate.enter.side_effect = lambda url: contextlib.nullcontext()
            def request(session, method, url, **kwargs):
                response = requests.Response()
                response.status_code = status
                response.url = url
                response.encoding = 'utf-8'
                response.headers['Retry-After'] = '120'
                params = kwargs.get('params')
                if params:
                    number = params['page']
                    response.url += '?' + urlencode(params)
                    response._content = page(number, [f'doc-{n}' for n in range((number-1)*10, min(number*10, 20))], number+1 if number == 1 else None)
                else:
                    response._content = b'<div class="publication-title"><h1>Scientific Research</h1></div><div class="publication-date">June 5, 2026</div>'
                return response
            with patch('delivery.health.scaht_persistence.RequestGate', return_value=gate), patch.object(requests.Session, 'request', request), patch('crawler.base_crawler.time.sleep'), contextlib.redirect_stdout(io.StringIO()):
                report = run_pilot(root/'out', root/'gate', evidence)
            return report, gate

    def test_real_sqlite_reopen_and_cancel_reconcile_exact_ids(self):
        report, _ = self.run_case()
        self.assertEqual(report['state'], 'bounded_native_save_cancel_reopen_verified')
        self.assertEqual([p['actual_new'] for p in report['phases']], [3, 3, 2, 12])
        self.assertEqual(report['phases'][2]['state'], 'cancelled')
        self.assertEqual(report['stored_count'], 20)
        self.assertFalse(report['full_collection_verified'])
        self.assertFalse(report['publisher_files_verified'])

    def test_access_denial_stops_without_retry_or_false_pass(self):
        report, _ = self.run_case(403)
        self.assertEqual(report['state'], 'incomplete')
        self.assertEqual(report['reason'], 'source_access_denied')
        self.assertEqual(len(report['requests']), 1)
        self.assertEqual(report['phases'][0]['state'], 'interrupted')
        self.assertEqual(report['phases'][0]['after'], 0)

    def test_rate_limit_persists_cooldown(self):
        report, gate = self.run_case(429)
        self.assertEqual(report['state'], 'host_cooldown_deferred')
        gate.defer.assert_called_once()
        self.assertEqual(len(report['requests']), 1)


if __name__ == '__main__':
    unittest.main()
