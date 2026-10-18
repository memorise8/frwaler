"""NR2-20261018: pure-mpg-de-search enumeration split (crawl() unchanged), memory-only.
POST JSON search (X1 POST listing, X2 nested body page value); papers from the records alone."""
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from tests.collector_split_helpers import load
from tests.nr2_a_split_helpers import NR2SplitChecks

SITE = 'pure-mpg-de-search'


class RegisteredSplit(NR2SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, _ = load(SITE)
        captured, payloads = [], []
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))

        def post_json(url, payload, **kw):
            payloads.append(payload)
            return json.loads(fixture['listing_body'] if len(payloads) == 1 else fixture['empty_body'])
        with patch.object(crawler, '_post_json', post_json), patch('time.sleep', lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=None)
        return captured, payloads

    def test_valid_empty_page_and_error_page_are_distinct(self):
        from crawler.enumeration.http import PermanentFailure, Response
        p, parser = self._parser()
        page = parser.parse(Response(200, {'Content-Type': 'application/json'}, self.fixture['empty_body'],
                                     p.listing['url']))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        with self.assertRaises(PermanentFailure):
            parser.parse(Response(200, {'Content-Type': 'application/json'}, '{"records": "x"}', p.listing['url']))

    def test_plan_body_matches_crawl_query(self):
        _, payloads = self.native()
        body = dict(self.plan['sites'][SITE]['listing']['body'])
        self.assertEqual('{page}', body.pop('offset'))
        first = dict(payloads[0]); self.assertEqual(0, first.pop('offset'))
        self.assertEqual(first, body)


if __name__ == '__main__':
    unittest.main()
