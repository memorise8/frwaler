"""ND-2: nibio-no-en enumeration listing/paper (crawl() unchanged), memory-only.
Identity keeps crawl()'s rule: post_number = panel uid as served (the site prefixes the
list position); the no-link fallback URL is crawl()'s page URL + #publication--<uid>-collapse."""
import unittest

from crawler.enumeration.http import Response
from tests.nd2_split_helpers import OnePageListChecks, run_split


class NibioSplit(OnePageListChecks, unittest.TestCase):
    SITE = 'nibio-no-en'

    def test_identity_and_fallback_url(self):
        papers = self.pipeline()
        self.assertEqual(['0f7fdd5c1-6043-4b88-9350-edbf0744046c', '15f5552e9-4880-40bc-a796-a6b15cef39dc'],
                         [p['post_number'] for p in papers])
        self.assertEqual('https://doi.org/10.21373/chs0-mk13', papers[0]['url'])
        self.assertEqual('https://www.nibio.no/en/publications?publicationType%5B%5D=e4b9b2bc-82be-42d1-80f5-f1515ad07e2e'
                         '&filters=1&q=&page=1#publication--15f5552e9-4880-40bc-a796-a6b15cef39dc-collapse',
                         papers[1]['url'])

    def test_shipped_plan_runs_end_to_end(self):
        fx = self.fixture
        result, docs = run_split(self.SITE, end=lambda url: Response(200, {'Content-Type': 'text/html'},
                                                                       fx['empty_body'], url))
        self.assertIn(result['status'], ('complete', 'complete_with_review'), result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))


if __name__ == '__main__':
    unittest.main()
