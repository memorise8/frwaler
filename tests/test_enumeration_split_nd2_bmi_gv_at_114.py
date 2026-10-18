"""ND-2: bmi-gv-at-114 enumeration listing (crawl() unchanged), memory-only.
Identity keeps crawl()'s rule (post_number = PDF folder, meta_url = start.aspx#item-<list position>);
several rows share one folder, as the registered crawler stores them (allow_shared_post_number)."""
import unittest

from tests.nd2_split_helpers import OnePageListChecks


class Bmi114Split(OnePageListChecks, unittest.TestCase):
    SITE = 'bmi-gv-at-114'

    def test_identity_rule_is_list_position(self):
        papers = self.pipeline()
        self.assertEqual(['1', '3'], [p['post_number'] for p in papers])
        self.assertEqual(['umfrage-2026', 'umfrage-2026'], [p['external_id'] for p in papers])
        self.assertTrue(papers[1]['url'].endswith('start.aspx#item-3'))

    def test_shipped_plan_runs_end_to_end(self):
        result, docs = self.run_wrap()
        self.assertEqual('complete_with_review', result['status'], result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['meta_url'] for d in docs}))


if __name__ == '__main__':
    unittest.main()
