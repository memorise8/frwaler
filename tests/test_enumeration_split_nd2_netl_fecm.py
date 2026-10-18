"""ND-2: netl-doe-gov-fecm-external-r-and- enumeration listing/paper (crawl() unchanged), memory-only.
The paper is crawl()'s no-OSTI branch (OSTI host not requested; user-delegated scope 10-19):
the original crawl() is run with OSTI answering nothing and must save the same rows."""
import unittest

from crawler.enumeration.http import Response
from tests.nd2_split_helpers import OnePageListChecks


class NetlFecmSplit(OnePageListChecks, unittest.TestCase):
    SITE = 'netl-doe-gov-fecm-external-r-and-'

    def test_identity(self):
        papers = self.pipeline()
        self.assertEqual(['FC26-05NT42587', 'FC26-05NT42588'], [p['post_number'] for p in papers])
        self.assertEqual('https://www.osti.gov/biblio/1735344', papers[0]['url'])

    def test_shipped_plan_runs_end_to_end(self):
        result, docs = self.run_wrap()
        self.assertIn(result['status'], ('complete', 'complete_with_review'), result)
        self.assertEqual(2, len(docs))


if __name__ == '__main__':
    unittest.main()
