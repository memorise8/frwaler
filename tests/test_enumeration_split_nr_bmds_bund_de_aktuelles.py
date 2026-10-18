"""NR-20261018: bmds-bund-de-aktuelles enumeration split (crawl() unchanged), memory-only.
Paging follows the page's own cHash-signed next link (ext:next_link, oldest_first_sort reader)."""
import unittest

from crawler.enumeration.http import Response
from tests.collector_split_helpers import run_split
from tests.nr_a_split_helpers import NRSplitChecks, NRSplitRunChecks


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = 'bmds-bund-de-aktuelles'


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'bmds-bund-de-aktuelles'

    def test_shipped_plan_completes_end_to_end(self):
        # The last page lists rows but has no a.page-forward: the cursor ends there.
        # (a page holding only the first row again: a different page fingerprint, no new document).
        head, rest = self.fixture['listing_body'].split('</li>', 1)
        body = head + '</li></ol></body></html>'
        result, docs = run_split(self.SITE, end=lambda url: Response(200, {'Content-Type': 'text/html'}, body, url))
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))
        self.assertEqual(2, len({d['meta_url'] for d in docs}))


if __name__ == '__main__':
    unittest.main()
