import unittest

from crawler.enumeration.http import Response
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks
from tests.collector_split_helpers import run_split


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = 'naacc-go-kr-web'


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = 'naacc-go-kr-web'

    def test_shipped_plan_completes_end_to_end(self):
        # crawl() stores the board's page-1 list URL as every paper's url, so meta_url is
        # shared; the registered dedup key stays unique through post_number (file id).
        end = lambda url: Response(200, {'Content-Type': 'text/html'}, self.fixture['empty_body'], url)
        result, docs = run_split(self.SITE, end=end)
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual({'SYNTH0001AAAA', 'SYNTH0002BBBB'}, {d['post_number'] for d in docs})
        self.assertEqual(1, len({d['meta_url'] for d in docs}))
