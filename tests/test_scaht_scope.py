import contextlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from delivery.health.scaht_scope import enumerate_scope, parse_listing


def page(number, ids, next_page=None):
    records = ''.join(f'<div class="publications-item"><p class="publications-date">5 Jun 2026</p><a href="/en/publications/{i}/">Title {i}</a></div>' for i in ids)
    link = f'<li><a class="page-link" href="?page={next_page}">»</a></li>' if next_page else ''
    return (records + f'<ul class="pagination"><li class="active"><span class="page-link">{number} (current)</span></li>{link}</ul>').encode()


class ScahtScopeTests(unittest.TestCase):
    def run_responses(self, bodies, **kwargs):
        with tempfile.TemporaryDirectory() as root:
            gate = Mock()
            gate.enter.side_effect = lambda url: contextlib.nullcontext()
            responses = [b if isinstance(b, dict) else {'status': 200, 'curl_exit': 0, 'headers': {}, 'body': b, 'body_complete': True} for b in bodies]
            with patch('delivery.health.scaht_scope.RequestGate', return_value=gate), patch('delivery.health.scaht_scope.fetch_once', side_effect=responses):
                result = enumerate_scope(Path(root)/'output', Path(root)/'gate', **kwargs)
                manifest_exists = (Path(root)/'output/manifest.json').exists()
            return result, manifest_exists, gate

    def test_two_traversals_and_after_last_are_required(self):
        traversal = [page(1, ['a', 'b'], 2), page(2, ['c']), page(3, [])]
        result, manifest, _ = self.run_responses(traversal * 2)
        self.assertEqual(result['enumerable_total'], 3)
        self.assertTrue(manifest)
        self.assertEqual(len(result['requests']), 6)
        self.assertFalse(result['full_collection_verified'])
        self.assertFalse(result['snapshot_guarantee'])

    def test_ambiguous_end_never_certifies_count(self):
        cases = ([page(1, [])], [page(1, ['a']), page(2, ['b'])],
                 [page(1, ['a'], 2), page(2, ['a'])])
        for bodies in cases:
            with self.subTest(bodies=bodies):
                result, manifest, _ = self.run_responses(bodies)
                self.assertEqual(result['state'], 'incomplete')
                self.assertIsNone(result['enumerable_total'])
                self.assertFalse(manifest)

    def test_source_change_and_budget_remain_incomplete(self):
        result, manifest, _ = self.run_responses([page(1, ['a']), page(2, []), page(1, ['b']), page(2, [])])
        self.assertEqual(result['failure_reason'], 'source_changed_between_complete_traversals')
        self.assertFalse(manifest)
        result, manifest, _ = self.run_responses([page(1, ['a'], 2)], max_pages=1)
        self.assertEqual(result['failure_reason'], 'page_budget_reached_not_exhausted')
        self.assertFalse(manifest)

    def test_partial_page_overlap_kept_as_lower_bound(self):
        traversal = [page(1, ['a', 'b'], 2), page(2, ['b', 'c']), page(3, [])]
        result, manifest, _ = self.run_responses(traversal * 2)
        self.assertEqual(result['state'], 'matching_traversals_with_overlap')
        self.assertEqual(result['observed_unique_count'], 3)
        self.assertEqual(result['list_occurrences'], 4)
        self.assertIsNone(result['enumerable_total'])
        self.assertTrue(manifest)

    def test_after_last_clamp_requires_identical_records_and_active_page(self):
        traversal = [page(1, ['a', 'b'], 2), page(2, ['c']), page(2, ['c'])]
        result, manifest, _ = self.run_responses(traversal * 2)
        self.assertEqual(result['enumerable_total'], 3)
        self.assertTrue(manifest)
        for after in (page(1, ['a', 'b']), page(2, ['changed']), b'<html>Error</html>'):
            result, manifest, _ = self.run_responses(traversal[:2]+[after])
            self.assertEqual(result['state'], 'incomplete')
            self.assertFalse(manifest)

    def test_single_pass_cannot_certify_total(self):
        result, manifest, _ = self.run_responses([page(1, ['a']), page(1, ['a'])], passes=1)
        self.assertEqual(result['state'], 'single_traversal_observed_requires_comparison')
        self.assertEqual(result['observed_unique_count'], 1)
        self.assertIsNone(result['enumerable_total'])
        self.assertTrue(manifest)

    def test_rate_limit_persists_cooldown(self):
        result, manifest, gate = self.run_responses([{'status': 429, 'headers': {'retry-after': '120'}, 'body': b'', 'body_complete': True}])
        self.assertEqual(result['state'], 'host_cooldown_deferred')
        gate.defer.assert_called_once()
        self.assertFalse(manifest)

    def test_wrong_page_and_scope_escape_fail(self):
        for body in (page(1, ['a'], 1), page(2, ['a']), page(1, ['a']).replace(b'/en/publications/a/', b'https://elsewhere.invalid/en/publications/a/')):
            with self.subTest(body=body), self.assertRaises(ValueError):
                parse_listing(body, 1)


if __name__ == '__main__':
    unittest.main()
