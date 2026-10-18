"""NR-20261018: difesa-it-il-ministro enumeration split (crawl() unchanged), memory-only.
The list is one static JSON file (no server paging): the page after the start page
repeats it, which the framework reads as wrap/clamp (last page = start page)."""
import unittest

from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.urls import build_url
from tests.collector_split_helpers import run_split
from tests.nr_a_split_helpers import NRSplitChecks, NRSplitRunChecks


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = 'difesa-it-il-ministro'

    def test_valid_empty_page_and_error_page_are_distinct(self):
        # A JSON list never ends on an empty page here (static file); an error page must
        # still never parse as an explicit end.
        p, parser = self._parser()
        url = build_url(p.listing['url'], p.listing.get('params') or {})
        try:
            err = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html><body>maintenance</body></html>', url))
        except (PermanentFailure, ValueError) as exc:
            self.assertTrue(str(getattr(exc, 'reason', exc)))
        else:
            self.assertEqual([], err.items)
            self.assertFalse(err.extra.get('explicit_empty', False))
        err = None
        try:
            err = parser.parse(Response(200, {'Content-Type': 'application/json'}, '{"error":"x"}', url))
        except PermanentFailure as exc:
            self.assertTrue(exc.reason.startswith('listing_parse_error:'), exc.reason)
        else:
            self.fail('JSON error object parsed as a listing')


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'difesa-it-il-ministro'

    def test_shipped_plan_completes_end_to_end(self):
        body = self.fixture['listing_body']
        result, docs = run_split(self.SITE, end=lambda url: Response(200, {'Content-Type': 'application/json'}, body, url))
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))


if __name__ == '__main__':
    unittest.main()
