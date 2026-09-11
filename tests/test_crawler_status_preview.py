import json
from pathlib import Path
import tempfile
import unittest

from scripts.audit.export_crawler_status_preview import classify, collect


class StatusPreviewTest(unittest.TestCase):
    def test_latest_failure_replaces_historical_success(self):
        with tempfile.TemporaryDirectory() as root:
            runs = [Path(root) / name for name in ('old', 'new')]
            for run, result in zip(runs, [
                {'id': 'source', 'samples': [{'title': 'Document'}], 'completed': True},
                {'id': 'source', 'samples': [], 'completed': True, 'error': 'ValueError: changed source'},
            ]):
                (run / 'results').mkdir(parents=True)
                (run / 'results/source.json').write_text(json.dumps(result))
            rows = collect([{'id': 'source', 'name': 'Source', 'source': '/app/crawler/source.py'}], runs, '2026-09-11')
            self.assertEqual(rows[0]['status'], 'code_error')
            self.assertEqual(len(rows[0]['history']), 2)
            self.assertEqual(rows[0]['samples'], [])

    def test_empty_and_missing_are_not_success(self):
        self.assertEqual(classify({'completed': True, 'samples': []}), 'zero')
        row = collect([{'id': 'new', 'name': 'New', 'source': '/app/new.py'}], [], '2026-09-11')[0]
        self.assertEqual(row['status'], 'unverified')
        self.assertIsNone(row['checked_date'])

    def test_partial_and_external_failures_are_distinct(self):
        self.assertEqual(classify({'samples': [{}], 'error': 'probe_timeout'}), 'partial')
        self.assertEqual(classify({'http_statuses': {'403': 3}}), 'blocked')
        self.assertEqual(classify({'http_statuses': {'502': 3}}), 'http_error')
        self.assertEqual(classify({'pdf_suppressed': 3}), 'pdf_limited')


if __name__ == '__main__':
    unittest.main()
