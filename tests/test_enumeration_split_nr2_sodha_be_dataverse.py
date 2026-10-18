"""NR2-20261018: sodha-be-dataverse enumeration split (crawl() unchanged), memory-only.
Search API rows (offset start=N); the framework fetches each row's dataset API JSON."""
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from tests.collector_split_helpers import load
from tests.nr2_a_split_helpers import NR2SplitChecks, NRSplitRunChecks

SITE = 'sodha-be-dataverse'


class RegisteredSplit(NR2SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, _ = load(SITE)
        captured, list_calls = [], []
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))

        def curl_json(url, context=None):
            if url in fixture['details']:
                return copy.deepcopy(fixture['details'][url])
            list_calls.append(url)
            return json.loads(fixture['listing_body'] if len(list_calls) == 1 else fixture['empty_body'])
        with patch.object(crawler, '_curl_json', curl_json), patch('time.sleep', lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=None)
        return captured, list_calls

    def test_search_row_is_passed_unchanged(self):
        crawler, _ = load(SITE)
        rows = crawler._enumeration_items(json.loads(self.fixture['listing_body']))
        self.assertTrue(all(crawler._ENUM_DETAIL_KEY in r for r in rows))
        paper = crawler._enumeration_paper(next(iter(self.fixture['details'].values())), rows[0])
        raw = json.loads(paper['metadata'])['raw_search_item']
        self.assertNotIn(crawler._ENUM_DETAIL_KEY, raw)


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_STATUS = 200

    def test_shipped_plan_completes_end_to_end(self):
        from urllib.parse import parse_qs, urlsplit
        from crawler.enumeration.http import Response
        from tests.collector_split_helpers import run_split
        listing, empty = self.fixture['listing_body'], self.fixture['empty_body']

        def end(url):
            start = parse_qs(urlsplit(url).query).get('start', ['0'])[0]
            return Response(200, {'Content-Type': 'application/json'}, listing if start == '0' else empty, url)
        result, docs = run_split(SITE, end=end)
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(2, len(docs))


if __name__ == '__main__':
    unittest.main()
