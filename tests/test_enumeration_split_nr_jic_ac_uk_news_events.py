"""NR-20261018: jic-ac-uk-news-events enumeration split (crawl() unchanged), memory-only.
List-only: each WP REST post becomes the paper (paper_method), no detail request.
Past the last page WordPress answers 400 rest_post_invalid_page_number (end_statuses)."""
import json
import unittest

from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.urls import build_url
from tests.collector_split_helpers import run_split, wp_past_last_page
from tests.nr_a_split_helpers import NRSplitChecks, NRSplitRunChecks


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = 'jic-ac-uk-news-events'

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p, parser = self._parser()
        url = build_url(p.listing['url'], p.listing.get('params') or {})
        with self.assertRaises(PermanentFailure) as ctx:
            parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html><body>maintenance</body></html>', url))
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'), ctx.exception.reason)
        with self.assertRaises(PermanentFailure):
            parser.parse(Response(200, {'Content-Type': 'application/json'}, json.dumps({'code': 'x'}), url))


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'jic-ac-uk-news-events'

    def test_shipped_plan_completes_end_to_end(self):
        result, docs = run_split(self.SITE, end=wp_past_last_page, total=2)
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))
        self.assertEqual(2, len({d['meta_url'] for d in docs}))

    def test_short_paper_does_not_silently_become_complete(self):
        fx = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        result, docs = run_split(self.SITE, fixture=fx, end=wp_past_last_page, total=2)
        # crawl() skips short posts; the run keeps them ledgered as failures, never saved.
        self.assertEqual([], docs)
        self.assertEqual(2, result['stats']['failed_permanent'], result)


if __name__ == '__main__':
    unittest.main()
