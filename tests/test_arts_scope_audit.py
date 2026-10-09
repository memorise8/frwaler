import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qsl, urlencode, urlsplit

from delivery.health.arts_scope_audit import ROOT, SITE, classify_records, run
from delivery.health.scaht_persistence import AuditStop


def record(n):
    return {'docid': n, 'halId_s': f'hal-{n}', 'uri_s': f'https://hal.science/hal-{n}',
            'title_s': [f'Research {n}'], 'abstract_s': ['a'*100],
            'producedDate_s': '2025', 'submittedDate_tdate': '2026-06-05T12:00:00Z'}


class ArtsAuditTests(unittest.TestCase):
    def test_exclusion_threshold_and_title_are_explicit(self):
        docs = [record(n) for n in range(1, 4)]
        docs[1]['abstract_s'] = ['a'*99]
        docs[2]['title_s'] = []
        rows = classify_records(docs)
        self.assertEqual([r['exclusion_reason'] for r in rows],
                         [None, 'abstract_shorter_than_100_chars', 'missing_title'])

    def test_colliding_native_keys_do_not_pass_unique_source_count(self):
        docs = [record(1), record(2)]
        docs[1]['halId_s'] = docs[0]['halId_s']
        with self.assertRaises(AuditStop):
            classify_records(docs)

    def run_case(self, *, status=200, change_after_inventory=False, contract_mismatch=False, raw_date='2025'):
        spec = importlib.util.spec_from_file_location('arts_contract_fixture', ROOT/'crawler/sites/custom'/f'{SITE}.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        captured = {}
        def capture(url, params):
            captured.update(endpoint=url, query_pairs=parse_qsl(urlencode(params, doseq=True)))
            return '{"response":{"numFound":0,"docs":[]}}'
        crawler = module.ArtsetmetiersHalScienceSearchCrawler(None)
        with patch.object(crawler, '_curl_get', side_effect=capture):
            crawler._fetch_page(0, module._ROWS)
        crawler._session.close()
        contract = dict(captured, site_id=SITE)
        docs = [record(n) for n in range(1, 13)]
        for doc in docs:
            doc['producedDate_s'] = raw_date
        docs[-1]['abstract_s'] = ['short']
        docs[-2]['title_s'] = []
        calls = []
        gate = Mock(); gate.enter.side_effect = lambda url: contextlib.nullcontext()
        def curl(argv, original, args, kwargs):
            params = dict(parse_qsl(urlsplit(argv[-1]).query))
            start, rows = int(params['start']), int(params['rows'])
            calls.append(start)
            page = [dict(d) for d in docs[start:start+rows]]
            if change_after_inventory and len(calls) > 2 and page:
                page[0]['title_s'] = ['Source changed']
            data = {'response': {'numFound': len(docs), 'docs': page}}
            return subprocess.CompletedProcess(argv, 0, json.dumps(data), ''), [status], 9999999999 if status == 429 else None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'contracts.json').write_text(json.dumps([contract]))
            (root/'boundary.json').write_text(json.dumps({
                'site_id': SITE, 'source_reported_total': len(docs),
                'boundary_state': 'sampled_boundaries_accessible_not_full_enumeration',
                'query_contract_sha256': 'wrong' if contract_mismatch else hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()}))
            with patch('delivery.health.arts_scope_audit.RequestGate', return_value=gate), \
                    patch('delivery.health.arts_scope_audit.curl_header_probe', side_effect=curl), \
                    patch('crawler.base_crawler.time.sleep'), contextlib.redirect_stdout(io.StringIO()):
                report = run(root/'out', root/'gate', root/'contracts.json', root/'boundary.json')
            db = root/'out/audit.sqlite'
            dates = None
            if db.exists():
                with sqlite3.connect(db) as conn:
                    dates = conn.execute('SELECT published_date,published_date_raw,published_date_precision FROM documents LIMIT 1').fetchone()
            return report, gate, dates

    def test_actual_sqlite_reopen_cancel_fill_and_no_new_reconcile(self):
        report, _, _ = self.run_case()
        self.assertEqual(report['state'], 'enumerated_and_code_eligible_metadata_reconciled')
        self.assertEqual([p['actual_new'] for p in report['phases']], [3, 3, 2, 2, 0])
        self.assertEqual(report['phases'][2]['state'], 'cancelled')
        self.assertEqual((report['source_unique_count'], report['code_eligible_count'], report['excluded_by_code']), (12, 10, 2))
        self.assertFalse(report['full_collection_verified'])
        self.assertFalse(report['files_verified'])
        self.assertFalse(report['exclusion_policy_accepted'])

    def test_source_change_stops_before_save(self):
        report, _, _ = self.run_case(change_after_inventory=True)
        self.assertEqual(report['reason'], 'source_records_changed_between_runs')
        self.assertEqual(report['phases'][0]['after'], 0)

    def test_year_only_date_survives_native_save_without_invented_day(self):
        report, _, dates = self.run_case()
        self.assertTrue(report['code_eligible_metadata_reconciled'])
        self.assertEqual(dates, (None, '2025', 'year'))

    def test_month_invalid_and_complete_dates_preserve_source(self):
        for raw, value, precision in (('2025-07', None, 'month'),
                                       ('2025-02-31', None, 'day'),
                                       ('2025-06-05T12:34:56Z', '2025-06-05', 'day')):
            with self.subTest(raw=raw):
                report, _, dates = self.run_case(raw_date=raw)
                self.assertTrue(report['code_eligible_metadata_reconciled'])
                self.assertEqual(dates, (value, raw, precision))

    def test_http_denial_stops_before_save(self):
        report, _, _ = self.run_case(status=403)
        self.assertEqual(report['reason'], 'source_http_or_transport_failure')
        self.assertEqual(report['phases'], [])
        self.assertEqual(len(report['requests']), 1)

    def test_rate_limit_persists_cooldown(self):
        report, gate, _ = self.run_case(status=429)
        self.assertEqual(report['state'], 'host_cooldown_deferred')
        gate.defer.assert_called_once()

    def test_different_boundary_contract_is_rejected(self):
        with self.assertRaises(ValueError):
            self.run_case(contract_mismatch=True)


if __name__ == '__main__':
    unittest.main()
