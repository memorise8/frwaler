"""ND-2: vtg-admin-ch-en enumeration listing (crawl() unchanged), memory-only.
Rows are crawl()'s papers for issues without a po.zem.ch table of contents (the reader host
is outside this site and is not requested; user-delegated scope 10-19)."""
import unittest

from tests.nd2_split_helpers import OnePageListChecks


class VtgSplit(OnePageListChecks, unittest.TestCase):
    SITE = 'vtg-admin-ch-en'

    def test_identity(self):
        papers = self.pipeline()
        self.assertEqual(['202601', '202504'], [p['post_number'] for p in papers])
        self.assertEqual(['AbC123', 'DeF456'], [p['external_id'] for p in papers])

    def test_shipped_plan_runs_end_to_end(self):
        result, docs = self.run_wrap()
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(2, len(docs))


if __name__ == '__main__':
    unittest.main()
